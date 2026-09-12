# Phase 20.1 Findings — Frozen Sequence Definitions and Inspection

## Implemented scope

- Additive scheduler migration `0005_sequence_definitions` (`PRAGMA user_version = 5`).
- Typed manifest, frozen entry, and prepared-sequence models under
  `src/ai_dev_loop/scheduler/domain/sequence.py`.
- Protected sequence artifacts under `artifacts/sequences/<sha256(sequence_id)>/` with
  lexical path inspection, symlink rejection, repository/artifact-root separation,
  private directory guarantees, and byte-preserving write-or-verify replay.
- `ai_dev_loop scheduler sequence prepare --manifest <path>` and
  `ai_dev_loop scheduler sequence status <sequence-id>` with text/JSON output.
- Placeholder `scheduler sequence start` that exits nonzero without mutation.
- Idempotent preparation keyed on frozen definition identity; optional controller
  provenance excluded from identity; explicit `--resubmission-id` for fresh replay.
- Reuse paths verify every persisted manifest, plan, prompt, config, and reviewer-binding
  artifact against recorded hashes and permission requirements before returning
  `reused_existing=True`.
- New prepared definitions verify the complete artifact set before SQLite insert; matching
  but world-readable partial artifacts are rejected rather than adopted.
- Deterministic preparation identity derived from the idempotency key; artifacts are written
  before SQLite commit with write-or-verify replay after interruption.
- Version-aware read-only schema validation preserves unmigrated v4 database access for
  scheduler status/list/history; sequence commands require schema version 5 explicitly.
- `compare_and_swap_sequence_state` preserves the complete frozen definition, including
  `sequence_id`, every `planned_run_id`, and controller provenance.
- `scheduler-sequence-manifest-v1.json` accepts omitted or explicit `null` cursor/workflow
  override objects; schema parity is enforced with JSON Schema validation in tests.

## Explicit non-goals preserved

- No `scheduler_runs`, reservations, effects, attempts, timers, or capacity rows.
- No Git subprocesses, repository admission, Cursor, Codex, commits, or worktree
  reservation during prepare.
- No sequence list/history, checkpoint commits, automatic advancement, or abort.

## Validation commands

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_1_sequence_corrections.py \
  tests/unit/scheduler/test_phase20_1_sequence_prepare.py \
  tests/unit/scheduler/test_protected_artifacts.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_v4_migration.py \
  tests/unit/scheduler/test_v5_migration.py \
  tests/integration/test_phase20_1_sequence_prepare.py \
  tests/integration/test_phase17_1_submit.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

All of the above passed in the correction workspace after the second Phase 20.1 review
pass (305 scheduler unit tests).

## Residual risks

- Prepare freezes definitions only; another scheduler run or manual repository edits may
  change the worktree before Phase 20.2 materializes the first phase.
- SQLite inserts and protected artifact writes are not one atomic filesystem/database
  transaction; replay relies on idempotency keys, deterministic IDs, and write-or-verify
  artifact semantics rather than cross-store atomicity.
- Preassigned `planned_run_id` values are not scheduler runs until Phase 20.2
  materialization; operators must not pass them to `scheduler status` expecting run
  state.
- `compare_and_swap_sequence_state` is hardened for Phase 20.2+ mutation paths but remains
  unused in Phase 20.1 command surfaces.
- Existing world-readable artifact directories outside the scheduler artifact roots are not
  proactively scanned; only scheduler-managed roots and artifact paths are secured or
  rejected at access time.

## Next phase boundary

Phase 20.2 owns `scheduler sequence start`, first-phase materialization, and repository
admission at sequence authorization time.
