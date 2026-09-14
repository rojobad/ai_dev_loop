# Phase 20.1 — Frozen Sequence Definitions and Inspection

## Goal

Introduce a durable, immutable definition for a linear sequence of scheduler
phases without materializing any scheduler run, reserving a repository, probing
Git, or launching an agent.

An operator must be able to prepare one sequence containing between 2 and 32
ordered phase entries. Preparation freezes every phase's exact plan, Cursor
prompt, resolved effective configuration, explicit Codex review model and
reasoning effort, executable paths, workflow limits, and non-final checkpoint
commit message under protected XDG artifacts. It also preassigns a stable future
run ID to every entry.

This phase establishes only the definition and read-only inspection boundary.
`scheduler sequence start`, run materialization, checkpoint commits, automatic
advancement, and sequence abort remain later Phase 20 work.

## Non-Goals

- Do not create rows in `scheduler_runs`, run events, effects, attempts, timers,
  capacity claims, or repository reservations.
- Do not implement `scheduler sequence start` or authorize any phase.
- Do not run repository admission, Cursor, Codex, Git status, Git staging, Git
  commits, hooks, signing, pushes, PR operations, or cleanup.
- Do not add branching, fan-out, fan-in, conditional phases, phase skipping,
  dynamic insertion, reordering, or mutation of a prepared sequence.
- Do not change standalone `scheduler submit`, `start`, `status`, `list`,
  `history`, `timeline`, `abort`, or `tick` behavior.
- Do not implement sequence checkpoint, reservation-transfer, recovery, abort,
  or finalization behavior from Phases 20.2–20.4.
- Do not alter Phase 19 Codex capacity classification, probing, waiting, resume,
  identity, privacy, or environment behavior.

## Scope

- Add typed sequence-definition domain/application contracts and a versioned
  manifest input model.
- Add one additive scheduler database migration for prepared sequences and
  ordered immutable entries. Historical databases and standalone runs must
  remain readable without rewriting rows.
- Add protected sequence artifact paths outside target repositories, parallel
  to but distinct from run artifact roots.
- Add `ai_dev_loop scheduler sequence prepare --manifest <path>` and
  `ai_dev_loop scheduler sequence status <sequence-id>` with text and JSON
  output.
- Allow the prepare command to use the existing repository/config selectors,
  optional controller provenance, output selector, and resubmission identity
  where applicable. The manifest path itself may be outside the repository,
  but every phase plan and prompt source must resolve safely inside the selected
  target repository.
- Freeze all entries during one prepare operation and expose only safe summary
  information through status.
- Add focused unit/integration/schema/privacy tests and a Phase 20.1 findings
  artifact.

## Out of Scope

- `src/ai_dev_loop/scheduler/application/tick.py`, attempt runners, Cursor and
  Codex workflow services, Git admission, Git staging, abort process control,
  timer assets, and user-systemd operations, except for imports or read-only
  compatibility adjustments strictly required by the new CLI surface.
- `ai_dev_loop.yaml`, the package-owned `ai-dev-loop-controller` and
  `ai-dev-loop-handoff` skills, the local planning/review skills, hook trust,
  Codex session bridges, global integrations, and real user XDG state.
- Any commit-capable Git adapter or update to the current no-commit governance
  rules; that control-plane change belongs to Phase 20.3.
- A public sequence list/history command. Phase 20.1 provides exact-ID status
  only.
- Deleting abandoned prepared definitions or their protected artifacts.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file.
- `archive/implementation-history/master-plan.md`, treating current code,
  tests, schemas, CLI help, and `docs/` as authoritative when historical
  contracts differ.
- The approved Phase 19 plan and its final findings, especially the durable
  `waiting_codex_capacity` state, reservation preservation, and no-migration
  boundary.
- Phase 17.1, 17.2, 17.7, 17.9, 17.10, and Phase 18 plans and corresponding
  findings for submission idempotency, reservations, migrations, privacy,
  output contracts, and operator-owned runs.
- `src/ai_dev_loop/cli.py` and `src/ai_dev_loop/commands/scheduler.py`.
- `src/ai_dev_loop/config.py`, `fresh_codex_reviewer.py`,
  `review_runtime.py`, and repository path/config resolution helpers.
- `src/ai_dev_loop/scheduler/application/submission.py`, `contracts.py`,
  `status.py`, and `safe_actions.py`.
- `src/ai_dev_loop/scheduler/domain/common.py`, `state.py`, and `events.py`.
- `src/ai_dev_loop/scheduler/infrastructure/sqlite_store.py`, every existing
  scheduler migration, `paths.py`, `protected_artifacts.py`, and
  `repository_target.py`.
- Current schemas under `src/ai_dev_loop/schemas/`, submission/store/artifact
  tests under `tests/unit/scheduler/`, and Phase 17.1/18 integration tests.
- Current CLI, configuration, privacy, observability, and full-flow docs.

Phase 19 must be implemented, independently reviewed, committed, and present as
the clean baseline before Phase 20.1 begins.

## Cursor Rules And Skills

- Follow `AGENTS.md` and all repository-local Cursor rules:
  `ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- The repository has no separate `.cursor/skills` directory. Do not modify or
  invoke the package-owned `ai-dev-loop-controller` or `ai-dev-loop-handoff`
  assets in this phase.
- Use temporary repositories, temporary XDG/config homes, injected clocks and
  ID factories, and fake executable paths in tests. Do not invoke real Cursor,
  Codex, systemd, account APIs, or global integrations.
- Keep implementation changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### Sequence source of truth

- A sequence is a first-class aggregate identified by `sequence_id`; it is not
  represented by a mutable `next_run_id` chain.
- Persist ordered entries with `sequence_id`, one-based `ordinal`, total count,
  unique human phase name, preassigned `planned_run_id`, immutable entry
  payload and payload hash. Enforce a linear order of 2–32 entries.
- The list order is authoritative. Do not accept user-supplied ordinals,
  predecessor IDs, successor IDs, branches, conditions, or mutable links.
- A prepared definition is immutable. Any changed input requires a distinct
  sequence; idempotent replay may reuse only an exactly identical prepared
  definition. Preserve the existing explicit resubmission-identity pattern for
  deliberately creating a fresh otherwise-identical sequence.
- Optional controller provenance is not authority and must not affect sequence
  idempotency, matching the Phase 18 run contract.

### Manifest contract

- Add a strict versioned YAML manifest with this conceptual public shape:

```yaml
schema_version: 1
name: "descriptive sequence name"
phases:
  - name: "phase identifier"
    plan_path: "repository/relative/plan.md"
    prompt_source_path: "repository/relative/prompt.txt"
    commit_message: "required for every non-final phase"
    cursor:
      command: null
      model: null
      output_format: null
    codex:
      command: null
      review_model: "required"
      review_reasoning_effort: "required"
      review_skill: null
    workflow:
      max_review_iterations: null
      cursor_timeout_minutes: null
      codex_timeout_minutes: null
```

- Optional per-phase values inherit from the selected repository configuration,
  but persistence must contain a fully resolved effective configuration for
  every entry. Do not persist unresolved inheritance as runtime authority.
- Continue freezing the exact configured Cursor/Codex executable paths using
  the existing submit-time resolution policy. Do not hash executable binaries
  or add tool updates.
- Require `commit_message` for entries 1 through N-1 and require it to be absent
  or null for entry N, because the final phase is intentionally left staged.
- Bound manifest, plan, prompt, configuration, and aggregate artifact sizes.
  Reject unknown fields, duplicate phase names, invalid types, unsafe paths,
  symlink escapes, empty content, missing files, invalid models/reasoning, and
  counts outside 2–32 before persistence.

### Freeze and artifact integrity

- `sequence prepare` freezes definitions, not the Git worktree. It must not
  invoke Git or reserve the repository. Git identity and cleanliness are first
  established when Phase 20.2 materializes the first run.
- Store artifacts under a deterministic protected root such as
  `artifacts/sequences/<safe-sequence-key>/`; never inside the target repository.
- Freeze the original manifest bytes plus a canonical resolved manifest and,
  per phase, exact plan bytes, exact prompt bytes, source config, effective
  config, and fresh reviewer binding evidence. Record relative artifact paths
  and SHA-256 hashes in typed persisted payloads.
- Reuse protected-artifact containment, no-symlink, bounded-write,
  write-or-verify, and user-only permission guarantees. Never reference one
  sequence through absolute XDG artifact paths in public state.
- Preparation must be all-or-nothing from the ledger's perspective. Make
  deterministic retry safe if a process dies around protected artifact writes;
  never reinterpret partial bytes or silently overwrite a conflicting artifact.

### Persistence and compatibility

- Use the next additive numbered migration. Do not edit checksums or SQL of
  applied migrations. Migration failure must roll back and leave the previous
  `PRAGMA user_version` intact.
- Prefer typed state/payload models and canonical hashes over free-form JSON.
  Keep database projection columns, payloads, schemas, store validation, and
  tests aligned.
- Do not add sequence provenance to scheduler run snapshots yet. No run exists
  in this phase.
- Preassigned run IDs are sequence-entry identifiers until materialization;
  they must not satisfy scheduler run lookup, status, start, abort, tick, or
  reservation queries.
- Existing databases, historical runs/events, and standalone submission
  idempotency must remain byte-compatible and behaviorally unchanged.

### Public output and phase honesty

- `sequence prepare` and `sequence status` may expose sequence ID, safe name,
  state, repository root, entry count, current/future ordinals, safe phase names,
  shortened hashes/IDs, timestamps, and the next supported action.
- Do not print prompt text, plan contents, full effective configs, raw manifest,
  full internal artifact paths, model process output, secrets, or authentication
  data. JSON output must be versioned and stable.
- The prepared state's safe next action must clearly say that sequence start is
  not implemented until Phase 20.2. If a placeholder `sequence start` command is
  registered for CLI discoverability, it must exit nonzero and perform no
  mutation.
- Update public documentation only for the behavior implemented in 20.1; do not
  describe automatic execution or checkpoint commits as available.

## Implementation Plan

1. Characterize existing submission freeze/idempotency, protected artifacts,
   migrations, output rendering, and repository-path tests before editing.
2. Define strict typed manifest authoring models separately from fully resolved
   immutable sequence-entry models. Reuse existing config validation and command
   resolution without routing through stdin or creating a run.
3. Add sequence/entry persistence with canonical hashes, preassigned future run
   IDs, optimistic versioning, deterministic idempotency, exact lookup, and the
   next additive migration. Keep the model extensible through versioned payloads
   rather than storing unvalidated dictionaries.
4. Extend protected artifact infrastructure with a sequence-specific root and
   bounded write-or-verify operations. Freeze all required artifacts and prove
   path containment, permissions, conflict detection, and replay behavior.
5. Implement `scheduler sequence prepare` and exact-ID `sequence status`, with
   stable text/JSON contracts and safe action wording that honestly stops at the
   Phase 20.1 boundary.
6. Add schemas and model/schema parity tests where persisted or public JSON
   shapes are introduced. Update only the CLI/config/privacy documentation
   necessary to explain preparation and frozen definitions.
7. Write
   `archive/implementation-history/findings/phase-20-1-frozen-sequence-definitions.md`
   with implemented contracts, migration evidence, validation commands, and
   residual risks. Do not claim Phase 20.2+ behavior.

## Testing Criteria

- **Manifest/model unit tests:** valid 2- and 32-phase manifests; rejection of
  0, 1, and 33 phases; duplicate/empty names; unknown keys; missing or invalid
  review model/reasoning; incorrect final/non-final commit messages; unsafe or
  escaping plan/prompt paths; missing, empty, oversized, and symlinked inputs.
- **Resolution/freeze unit tests:** per-phase overrides and repository defaults
  resolve independently; Cursor and Codex may vary by phase; executable paths,
  exact plan/prompt bytes, source/effective configs, reviewer input, hashes, and
  ordering are frozen; later source-file changes do not alter stored artifacts.
- **Persistence/migration tests:** clean migration from the Phase 19 baseline,
  migration rollback/checksum behavior, old database readability, unique
  sequence identity, unique `(sequence_id, ordinal)` and planned run IDs,
  canonical payload hash verification, optimistic version checks, and rejected
  corrupted rows.
- **Idempotency tests:** identical prepare reuses the same sequence; optional
  controller provenance does not change identity; explicit resubmission creates
  one fresh sequence and replaying the same resubmission ID reuses it; changed
  phase order/content/config creates a different identity.
- **Artifact/privacy tests:** sequence artifacts stay outside the repository,
  use safe deterministic paths and private permissions, reject symlink/conflict
  attacks, survive idempotent replay, and do not leak plan/prompt/config contents
  through status, errors, or JSON summaries.
- **No-side-effect integration tests:** prepare/status invoke no Git or agent
  subprocess, create no scheduler run/reservation/effect/attempt/timer/capacity
  holder, and do not modify the target worktree. Existing standalone submission
  and conflicting-worktree tests remain unchanged.
- **CLI contract tests:** text/JSON schema, exact-ID not-found behavior, safe
  truncated identifiers, honest Phase 20.1 next action, nonzero placeholder
  start if present, and read-only status under concurrent access.

All automated tests must use temporary XDG/config roots and fake executables.
Do not use the active repository as a target and do not invoke real models,
systemd, hooks, credentials, or external services.

## Validation

Run focused tests first, followed by the scheduler regression suite and static
checks. At minimum, adapt paths to the final test names and run:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_sqlite_store.py \
  tests/unit/scheduler/test_protected_artifacts.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_phase20_1_sequence_prepare.py \
  tests/integration/test_phase20_1_sequence_prepare.py \
  tests/integration/test_phase17_1_submit.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

Do not run the real scheduler timer, Cursor, Codex, hooks, or a model-backed
acceptance test.

## Risks Or Recovery Notes

Sequence preparation deliberately does not reserve or freeze Git. Another run
may use or change the repository before `sequence start`; Phase 20.2 must admit
the actual clean baseline present when it materializes the first run. Frozen
plans and prompts remain unchanged even if their repository source files later
change.

Because protected artifact writes and SQLite cannot be one filesystem/database
transaction, retry must be content-addressed and write-or-verify. A partial
artifact tree must never become a different prepared definition, and corrupted
or conflicting bytes must block rather than be replaced.

The repository's existing Cursor rules still categorically prohibit commits.
Phase 20.1 must not weaken them in anticipation of Phase 20.3.

## OpenQuestions

None.
