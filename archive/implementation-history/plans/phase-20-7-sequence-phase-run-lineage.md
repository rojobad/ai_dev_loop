# Phase 20.7 — Sequence Phase Run Lineage

## Goals

- Extend the Phase 20 sequence persistence model so one frozen phase ordinal can
  retain an ordered lineage of more than one scheduler run without changing the
  frozen sequence definition.
- Distinguish the planned run, current run, accepted run, and superseded terminal
  runs for every materialized sequence phase.
- Limit the new lineage to the accepted sequence implementation and the existing
  same-reviewer recovery successor that Phase 20.8 will make sequence-aware.
- Provide a backward-compatible, fully validated persistence foundation for the
  sequence-aware manual review retry implemented in Phase 20.8.

## Non-Goals

- Do not make `scheduler review retry` sequence-aware in this phase.
- Do not create a new run, reactivate a blocked sequence, advance an ordinal,
  create a checkpoint commit, or change automatic scheduler behavior.
- Do not restore, import, adapt, or reimplement the abandoned Phase 20.6
  fresh-review recovery work. It is absent from the accepted baseline and is not
  a prerequisite for this phase.
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
- Add an atomic, idempotent, write-once terminal-resolution operation and invoke
  it from the existing accepted, residual-risk, blocked, aborted, and finalization
  transitions so real flows populate the authoritative lineage without changing
  which transitions occur.
- Reserve a closed same-reviewer retry attempt kind and the exact store APIs that
  Phase 20.8 will use, without creating a successor or changing runtime behavior
  in this phase.
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
- The committed Phase 20.5 implementation, plan, findings, schemas, and capacity
  recovery behavior.
- The archived Phase 20.6 plan and abandonment history only to confirm that no
  Phase 20.6 runtime, migration, schema, command, or compatibility behavior may
  be assumed or imported.
- `src/ai_dev_loop/scheduler/domain/sequence.py`,
  `sequence_lifecycle_validation.py`, sequence reducers/contracts, run recovery
  context models, and public safe-action models.
- `src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py`, all scheduler
  migrations, protected artifact helpers, and versioned JSON schemas.
- Sequence prepare/start/materializer/handoff/reconcile/report/status/abort and
  existing same-reviewer retry/recovery services.
- Existing Phase 20 unit/integration tests and
  `tests/unit/scheduler/test_schema_readonly_historical.py`.

The clean execution baseline must contain the independently reviewed and
committed Phase 20.5 implementation. Phase 20.6 is intentionally not
implemented and must not be treated as a prerequisite.

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
- Attempt kinds are a closed typed set containing only the planned run and the
  same-reviewer retry kind needed by Phase 20.8. Do not persist arbitrary strings
  or reserve a Phase 20.6 fresh-review kind.
- A materialized ordinal has exactly one current leaf. An accepted run, when
  present, must be that ordinal's authenticated leaf and have an advancing
  accepted outcome. Earlier terminal attempts remain immutable and visible.
- Terminal resolution is written once from authenticated run/sequence transition
  authority. Replays verify the exact existing value; a contradictory second
  outcome fails closed. Accepted and residual-risk outcomes set the accepted
  leaf, while blocked/aborted outcomes resolve the attempt without fabricating
  acceptance.

### Compatibility and schema discipline

- Add a forward-only migration and an explicit read strategy for historical
  Phase 20 sequence rows. Do not require new tables/columns when opening an old
  database read-only.
- Historical one-run entries must deterministically project to generation 1
  without rewriting their frozen definition or inventing recovery facts.
- Existing accepted one-run sequence rows project only their authenticated
  planned run and terminal outcome. Never infer extra generations from summaries,
  filenames, `last_error`, or mutable artifacts.
- Validate complete aggregate invariants whenever persisted sequence state is
  read, not only before writes. Invalid persisted combinations fail closed with
  privacy-safe errors.
- Update typed models, reducers, migrations, stores, JSON schemas, schema exports,
  artifact readers/writers, and tests together. Schema parity tests must validate
  representative complete payloads and invalid nullable/cross-field cases, not
  property-name proxies.
- Authority queries must not use display pagination or a fixed first-page limit.
- Load and validate the complete unpaginated lineage for a sequence. Extra,
  future, orphaned, cross-ordinal, or cross-sequence rows are persisted
  corruption and must never remain invisible merely because an ordinal is not in
  `materialized_entries`.

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
- The abandoned Phase 20.6 snapshots are not compatibility inputs. If any staged
  or live code begins importing their tables, models, migrations, commands, or
  Git authority, stop and remove that dependency rather than reconstructing it.

## Implementation Plan

1. Characterize the committed Phase 20.5 baseline, current single-run sequence
   persistence, existing same-reviewer successor lineage, and all current Phase
   20 sequence aggregate invariants with focused regression tests.
2. Define the versioned run-attempt and per-ordinal execution models, including
   closed attempt kinds, generation/source rules, current leaf, accepted leaf,
   and residual-risk consistency.
3. Update sequence state variants and centralized lifecycle validation so every
   persisted read validates complete definition/materialization/attempt
   consistency.
4. Add the forward migration and store APIs for full, unpaginated lineage reads,
   idempotent insertion, exact existing-row verification, and versioned CAS.
5. Add backward-compatible adapters for historical single-run Phase 20 rows;
   do not recognize or import abandoned Phase 20.6 WIP shapes.
6. Update sequence prepare/start/materialization to write the new generation-1
   projection, then wire atomic write-once terminal resolution into the existing
   accepted, residual-risk, blocked, aborted, and finalization transitions without
   changing their lifecycle behavior.
7. Load and validate every stored lineage row for the sequence, rejecting extra,
   future, orphaned, cross-ordinal, and cross-sequence rows.
8. Update JSON schemas and schema/model parity tests using identical complete
   valid and invalid payloads, including nullable/generation/source/recovery and
   current/accepted-leaf constraints plus historical read-only database fixtures.
9. Add privacy-safe internal inspection fixtures and regression coverage proving
   no Git/process/recovery command behavior changed.
10. Write
   `archive/implementation-history/findings/phase-20-7-sequence-phase-run-lineage.md`
   with exact validation evidence and residual risks.

## Testing Criteria

- **Domain tests:** generation 1 equals `planned_run_id`; ordered multi-generation
  chains; exact source links; unique run IDs; no cycles/forks/gaps; one current
  leaf; accepted leaf/outcome consistency; residual-risk consistency; rejection
  of future ordinals and cross-sequence/cross-ordinal lineage.
- **Schema tests:** complete model-to-JSON-schema parity for every changed state;
  missing, null, duplicate, malformed, and contradictory fields fail identically.
- **Migration tests:** fresh database, upgrade from the current accepted Phase
  20.5 baseline, read-only opening of representative older databases, interrupted
  migration
  rollback, and repeated initialization are deterministic.
- **Store/CAS tests:** exact insert/replay, conflicting replay refusal, complete
  definition comparison, concurrent duplicate insertion, CAS loser behavior,
  and unbounded authoritative lookup beyond display-page sizes.
- **Terminal-resolution tests:** accepted, residual-risk, blocked, aborted, and
  final-phase transitions resolve generation 1 atomically and idempotently;
  contradictory replay fails; accepted-run projection comes only from an
  advancing stored outcome; historical checkpointed/finalized rows migrate
  without inventing results.
- **Persisted-corruption tests:** complete lineage reads reject extra, future,
  orphaned, cross-ordinal, and cross-sequence rows even when they are not named by
  `materialized_entries`.
- **Compatibility tests:** old one-run sequences behave unchanged; existing
  same-reviewer successor rows remain readable without being adopted by a
  sequence in this phase; abandoned Phase 20.6 shapes are not introduced.
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

Use the actual committed Phase 20.5 focused filenames when they differ and
record the exact commands in the findings artifact.

## Risks Or Recovery Notes

- The primary risk is creating two sources of truth: the original materialized
  entry and a new attempt list. Use one authoritative representation plus strict
  derived projections, or validate duplicated fields on every read.
- A migration that invents a recovery generation from untrusted or abandoned
  data would corrupt audit lineage. Preserve only exact accepted source rows.
- Do not broaden a schema-only phase into recovery behavior merely to make tests
  convenient. Honest boundary errors are preferable until Phase 20.8.
- If historical data is contradictory, preserve it for diagnosis and fail closed;
  do not normalize or overwrite it automatically.

## OpenQuestions

None.
