# Phase 17.1 — Central ledger and A/B submission

## Goal

Establish the new, sole durable authority for final local A/B runs:
`$XDG_STATE_HOME/ai_dev_loop/engine.sqlite3` plus one protected artifact tree.
Add a no-agent A/B `scheduler submit` operation that freezes a run for later
explicit controller authorization.

## Non-Goals

- Do not run preflight, probes, Cursor, Codex, Git staging, systemd, or a tick.
- Do not delete old state, remove old commands, or change PR-review behavior.
- Do not add a legacy-state importer, state.json projection, compatibility
  reader, or automatic data migration.

## Scope

- Add typed scheduler domain models, strict JSON schemas, SQLite bootstrap and
  migrations, protected artifact storage, and read-only status/list projection.
- Add `ai_dev_loop scheduler submit` for A/B runs and the minimum CLI wiring.
- Freeze the exact reviewer session/runtime, exact controller A identity,
  repository/path bindings, plan/prompt bytes, effective settings, and limits.
- Create/reuse an identical submitted run and reject a conflicting active
  resolved worktree.

## Out of Scope

- `scheduler start`, `scheduler tick`, process attempts, retries, cancellation,
  systemd installation, agent output, or target repository mutation.
- Changes to `ai_dev_loop.yaml`, SessionStart hooks, global skills, or desktop
  bridge code.

## Required Context

Read the Phase 17 master plan, all `.cursor/rules/*.mdc`, `state.py`,
`paths.py`, `commands/prepare.py`, `review_runtime.py`,
`pr_review_v2/infrastructure/{sqlite_store,paths}.py`, protected-artifact
helpers, `tests/conftest.py`, and schema/config tests before editing.

## Cursor Rules And Skills

Follow all eight `.cursor/rules/*.mdc` files. Follow
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` for any docs;
the staged-review skill is for later independent review only. No `AGENTS.md` or
`.cursor/skills/` directory exists.

## Architecture Guardrails

- `engine.sqlite3` is new and generic; do not extend or read
  `pr-review-v2/engine.sqlite3`.
- SQLite snapshots/events/effects/artifact hashes are sole truth. Use WAL,
  `FULL` sync, foreign keys, schema audit/checksum, `BEGIN IMMEDIATE`, CAS, and
  strict typed serialization.
- Submission writes only central state and protected immutable artifacts outside
  the target repo. It must never create `state.json` or an old run directory.
- Require distinct exact controller and reviewer IDs; never use `--last` or
  infer a session. Default CLI output must redact full IDs and sensitive bytes.
- Do not create a database row that references a partial or unverified artifact.

## Implementation Plan

1. Add isolated scheduler path, domain, store, artifact, and application layers.
   Keep domain/reducer free of SQLite, process, filesystem, and clock I/O.
2. Define migration-audited tables for runs, state snapshots/version, journal
   events, effects/timers (unused but structurally ready), claims, attempts, and
   repository reservations. Add only fields justified by later audit/recovery.
3. Define and schema-test `SubmittedRunContext` and `SubmittedState`; freeze
   exact plan/prompt copies and hashes, effective config, repository binding,
   controller/reviewer identity, and Cursor/Codex effective runtime.
4. Add `scheduler submit` using the existing safe input/session validation
   primitives where semantics fit. It reads stdin once, validates confinement and
   duplicate keys, writes snapshots, then atomically inserts/reuses `queued`.
5. Add scheduler `status` and `list` read models for submitted runs. They expose
   only redacted IDs, state, timestamps, and safe action; they do not inspect
   legacy run roots.
6. Update only factual CLI/state-layout docs for the submitted-but-not-executing
   boundary. Do not claim the scheduler can run agents yet.

## Testing Criteria

- Unit/schema: model/schema alignment, artifact hash/path/symlink rejection,
  permissions, migration bootstrap/checksum/newer-version/fault rollback, CAS,
  and foreign-key invariants.
- Command/integration: fake session rollout plus disposable repository prove
  submit writes no target mutation, no subprocess call, no probe, and no agent;
  exact duplicate reuses a run; same-worktree conflict rejects; status/list are
  redacted and deterministic.
- Use temporary XDG/HOME and fake CLIs only. Add focused tests under
  `tests/unit/scheduler/` and `tests/integration/test_phase17_1_*.py`.

## Validation

Run focused Phase 17.1 tests, then:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration/test_phase17_1_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

## Risks Or Recovery Notes

The principal risk is accidentally retaining a second mutable authority. A
failure before SQLite commit may leave an unreferenced content-addressed object;
that is safe. A committed run with an unverified reference is not. Do not start
the next phase until submit can prove it is side-effect free.

## OpenQuestions

None. The master plan freezes the storage location, A/B-only scope, controller
binding, and no-compatibility policy.
