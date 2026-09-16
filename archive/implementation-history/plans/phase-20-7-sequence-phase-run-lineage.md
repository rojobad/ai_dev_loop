# Phase 20.7 — Sequence Phase Run Lineage

## Goals

- Extend the Phase 20 sequence persistence model so one frozen phase ordinal can
  retain an ordered lineage of more than one scheduler run without changing the
  frozen sequence definition.
- Distinguish the planned run, current run, accepted run, and superseded terminal
  runs for every materialized sequence phase.
- Normalize the Phase 20.6 sequence-recovery resolution into the same typed
  lineage used by later same-reviewer recovery, while preserving all Phase 20.6
  behavior and authority.
- Provide a backward-compatible, fully validated persistence foundation for the
  sequence-aware manual review retry implemented in Phase 20.8.

## Non-Goals

- Do not make `scheduler review retry` sequence-aware in this phase.
- Do not create a new run, reactivate a blocked sequence, advance an ordinal,
  create a checkpoint commit, or change automatic scheduler behavior.
- Do not revise or weaken the Phase 20.6 fresh-review recovery workflow,
  managed-worktree authority, automatic integration, or cleanup rules.
- Do not add automatic retry, retry on every failure, replacement reviewers,
  model fallback, dynamic sequence extension, skipped/reordered phases, or
  parallel phases.
- Do not change which outcomes advance a sequence or how a final ordinary phase
  reaches `awaiting_finalization`.

## Scope

- Add typed, versioned phase-execution and run-attempt lineage models for
  materialized sequence entries.
- Keep `FrozenSequenceEntry.planned_run_id` immutable and make it the required
  first attempt for newly started sequences.
- Record, at minimum, attempt generation, run ID, source run ID when applicable,
  recovery kind, materialization time, and safe terminal-resolution metadata.
- Add explicit current-run and optional accepted-run projections with aggregate
  validators that prove they agree with the ordered attempt lineage.
- Adapt Phase 20.6 sequence recovery-resolution persistence to project its source
  and recovery runs into the new lineage without changing its commands or Git
  behavior.
- Add a forward migration, store operations, model/JSON-schema parity, historical
  read compatibility, and focused tests.
- Add internal APIs needed by Phase 20.8 to authenticate the current leaf and
  compare-and-swap a future replacement, but do not call them from public
  recovery commands yet.

## Out of Scope

- Git mutations, staging, commits, refs, managed worktree creation/removal,
  repository reservation transfer, process launch, Cursor/Codex invocation, and
  real scheduler timer operations.
- `ai_dev_loop.yaml`, model catalogs/defaults, capacity classification, review
  budgets, prompts, review-result schemas, global integrations, SessionStart
  hooks/bridges, package-owned skills, credentials, GitHub, PRs, and remotes.
- Editing the archived Phase 20.5 or Phase 20.6 plan artifacts.
- User-facing sequence recovery behavior, broad status redesign, and final
  end-to-end documentation; those belong to Phases 20.8 and 20.9.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file.
- Current `/docs`, treating current code, schemas, tests, and CLI help as more
  authoritative than archived history when they differ.
- Phase 20.1–20.4 plans and findings, especially immutable definitions, lazy
  materialization, checkpoint handoff, lifecycle validation, historical reads,
  and deterministic reports.
- Phase 20.1.1 plan/findings and the current reviewer-recovery lineage and
  successor lookup implementation.
- The committed Phase 20.5 and Phase 20.6 implementations, plans, findings,
  migrations, schemas, recovery aggregate, sequence recovery resolution, and
  automatic integration behavior.
- `src/ai_dev_loop/scheduler/domain/sequence.py`,
  `sequence_lifecycle_validation.py`, sequence reducers/contracts, run recovery
  context models, and public safe-action models.
- `src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py`, all scheduler
  migrations, protected artifact helpers, and versioned JSON schemas.
- Sequence prepare/start/materializer/handoff/reconcile/report/status/abort and
  Phase 20.6 recovery services.
- Existing Phase 20 unit/integration tests and
  `tests/unit/scheduler/test_schema_readonly_historical.py`.

The clean execution baseline must contain the independently reviewed and
committed Phase 20.6 implementation.

## Cursor Rules And Skills

- Follow `AGENTS.md` and all repository-local rules:
  `.cursor/rules/ai-dev-loop-governance.mdc`,
  `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- The repository has no `.cursor/skills` directory. Do not modify or invoke the
  package-owned handoff/controller skills, this planning skill, or the staged
  review skill.
- Use typed fixtures and temporary SQLite/XDG state only. Do not invoke real
  Cursor/Codex, Git mutation, the systemd timer, hooks, credentials, or global
  installation.
- Leave implementation changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### Frozen definition and lineage source of truth

- The prepared sequence definition remains immutable. Never replace
  `planned_run_id`, rewrite an entry hash, or add mutable attempt state to the
  frozen identity payload.
- Store mutable execution lineage in versioned sequence state or normalized
  ledger rows with one clear authoritative representation. Any duplicate JSON
  projection must be derived and validated, not independently authoritative.
- The first attempt for an ordinal is the exact planned run. Later attempts use
  contiguous generations and must point to the immediately preceding source
  leaf; forks, cycles, duplicate run IDs, duplicate generations, and cross-
  ordinal/cross-sequence references fail validation.
- Recovery kinds are a closed typed set. Include the Phase 20.6 fresh-review
  recovery kind and reserve the same-reviewer retry kind needed by Phase 20.8;
  do not persist arbitrary strings.
- A materialized ordinal has exactly one current leaf. An accepted run, when
  present, must be that ordinal's authenticated leaf and have an advancing
  accepted outcome. Earlier terminal attempts remain immutable and visible.

### Compatibility and schema discipline

- Add a forward-only migration and an explicit read strategy for historical
  Phase 20 sequence rows. Do not require new tables/columns when opening an old
  database read-only.
- Historical one-run entries must deterministically project to generation 1
  without rewriting their frozen definition or inventing recovery facts.
- Current Phase 20.6 recovery resolutions must project to exact source/recovery
  generations from authenticated stored IDs and outcomes, never from summaries,
  filenames, `last_error`, or mutable artifacts.
- Validate complete aggregate invariants whenever persisted sequence state is
  read, not only before writes. Invalid persisted combinations fail closed with
  privacy-safe errors.
- Update typed models, reducers, migrations, stores, JSON schemas, schema exports,
  artifact readers/writers, and tests together. Schema parity tests must validate
  representative complete payloads and invalid nullable/cross-field cases, not
  property-name proxies.
- Authority queries must not use display pagination or a fixed first-page limit.

### Persistence and privacy

- Compare-and-swap operations must compare the complete immutable and versioned
  execution definition relevant to the mutation, not a normalized subset.
- Any lineage artifact is atomically write-or-verify, bounded, under its exact
  XDG root, protected against lexical symlink traversal before resolution, and
  user-only where supported. Prefer ledger rows when no sensitive artifact is
  needed.
- Public errors/status fixtures may expose only safe run prefixes, ordinals,
  generation counts, typed kinds, and safe actions. Do not expose full session
  IDs, prompts, patches, reviews, provider prose, raw events, Git identity, or
  absolute managed-worktree paths.
- Pydantic validation details containing original payload values must not escape
  into ordinary CLI/log surfaces.

### Phase boundary

- This phase may persist and read the richer model but must preserve existing
  runtime behavior exactly. No command may create a second attempt solely because
  the schema can represent one.
- Phase 20.6 recovery writes may be adapted to the new model only to preserve
  their already-approved behavior and idempotency.
- If the committed Phase 20.6 representation cannot be migrated without changing
  its safety semantics, stop and record the conflict in `OpenQuestions` before
  implementing a substitute design.

## Implementation Plan

1. Characterize the committed Phase 20.6 sequence/recovery persistence and all
   current Phase 20 sequence aggregate invariants with focused regression tests.
2. Define the versioned run-attempt and per-ordinal execution models, including
   closed recovery kinds, generation/source rules, current leaf, accepted leaf,
   and residual-risk consistency.
3. Update sequence state variants and centralized lifecycle validation so every
   persisted read validates complete definition/materialization/attempt
   consistency.
4. Add the forward migration and store APIs for full, unpaginated lineage reads,
   idempotent insertion, exact existing-row verification, and versioned CAS.
5. Add backward-compatible adapters for historical single-run Phase 20 rows and
   for committed Phase 20.6 recovery-resolution rows.
6. Update sequence prepare/start/materialization and Phase 20.6 persistence to
   write the new generation-1/recovery projections without changing behavior.
7. Update JSON schemas and schema/model parity tests using full valid and invalid
   payloads, including historical read-only database fixtures.
8. Add privacy-safe internal inspection fixtures and regression coverage proving
   no Git/process/recovery command behavior changed.
9. Write
   `archive/implementation-history/findings/phase-20-7-sequence-phase-run-lineage.md`
   with exact validation evidence and residual risks.

## Testing Criteria

- **Domain tests:** generation 1 equals `planned_run_id`; ordered multi-generation
  chains; exact source links; unique run IDs; no cycles/forks/gaps; one current
  leaf; accepted leaf/outcome consistency; residual-risk consistency; rejection
  of future ordinals and cross-sequence/cross-ordinal lineage.
- **Schema tests:** complete model-to-JSON-schema parity for every changed state;
  missing, null, duplicate, malformed, and contradictory fields fail identically.
- **Migration tests:** fresh database, upgrade from the Phase 20.6 schema,
  read-only opening of representative older databases, interrupted migration
  rollback, and repeated initialization are deterministic.
- **Store/CAS tests:** exact insert/replay, conflicting replay refusal, complete
  definition comparison, concurrent duplicate insertion, CAS loser behavior,
  and unbounded authoritative lookup beyond display-page sizes.
- **Compatibility tests:** old one-run sequences behave unchanged; committed
  Phase 20.6 standalone and sequence recovery projections preserve exact source,
  replacement, accepted outcome, residual risk, and report inputs.
- **Boundary tests:** public `review retry`, sequence reconcile/handoff/abort, and
  scheduler tick do not create or adopt a new attempt in this phase.
- **Privacy tests:** validation failures, status, history, events, and logs do not
  expose protected payload values or sensitive artifacts.

All tests are hermetic and use temporary state. They must not invoke real agents,
real provider accounts, real repositories, Git mutation, or workstation services.

## Validation

Run focused tests first, followed by the complete scheduler and project checks:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_schema_readonly_historical.py \
  tests/unit/scheduler/test_phase20_1_sequence_prepare.py \
  tests/unit/scheduler/test_phase20_2_sequence_start.py \
  tests/unit/scheduler/test_phase20_4_sequence_lifecycle.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

If committed Phase 20.6 uses different focused filenames, run their actual
equivalents and record the exact commands in the findings artifact.

## Risks Or Recovery Notes

- The primary risk is creating two sources of truth: the original materialized
  entry and a new attempt list. Use one authoritative representation plus strict
  derived projections, or validate duplicated fields on every read.
- A migration that silently treats a Phase 20.6 recovery as an ordinary planned
  run would erase audit lineage. Preserve exact typed source and recovery IDs.
- Do not broaden a schema-only phase into recovery behavior merely to make tests
  convenient. Honest boundary errors are preferable until Phase 20.8.
- If historical data is contradictory, preserve it for diagnosis and fail closed;
  do not normalize or overwrite it automatically.

## OpenQuestions

None.
