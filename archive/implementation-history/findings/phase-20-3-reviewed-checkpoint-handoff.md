# Phase 20.3 Findings — Reviewed Checkpoint Commits and Atomic Phase Handoff

## Implemented scope

- Non-terminal `checkpoint_pending` run state for accepted non-final sequence phases.
- `awaiting_finalization` sequence state for accepted final phases with staged changes retained.
- Immutable `SequenceCheckpointIntent`, immutable `SequenceCheckpointTrustedTree`, mutable
  `SequenceCheckpointEvidence`, and `SequenceCheckpointResult` protected artifacts.
- Injectable `ProductionGitCheckpointPort` using bounded checkpoint Git subprocesses (reads and
  mutations) with command-local hook suppression, unsigned commits, and per-command lease
  deadline budgeting via `CheckpointGitDeadline`.
- Durable `scheduler_checkpoint_holds` reconciliation rows (schema v7) integrated with
  `has_unresolved_abort_hold`, abort cleanup, and checkpoint abort coordination.
- Mutation fencing at `commit-tree` and `update-ref` boundaries coordinated with tick lease,
  abort, and reservation ownership; durable `checkpoint_cas_authorized` holds persisted in the
  same immediate transaction as the final abort, lease, state, and reservation checks before
  `update-ref`; durable reconciliation holds when Git advanced but ledger handoff did not
  complete.
- Orphan-intent recovery reuses verified on-disk bytes and frozen bindings without regenerating
  JSON or timestamps; orphan promotion authenticates `commit_message`, successor identity,
  `branch_ref`, and other path/identity bindings against frozen sequence and review context.
  Missing trusted-tree/evidence artifacts are reconstructed from authenticated live repository
  state when no mutation has occurred.
- Expected parent HEAD validation for every non-final phase ordinal using authenticated admission
  HEAD (ordinal 1) or fully authenticated predecessor `result.json` bindings (ordinal 2+), not
  unverified materialized metadata alone.
- Strengthened postconditions: empty index matching committed tree and clean worktree.
- Atomic evidence persistence via protected-artifact replace with commit identity verification
  against the immutable trusted reviewed tree.
- `SequenceHandoffService` integrated into Codex review completion and scheduler tick.
- Atomic ledger handoff with direct reservation transfer; residual-risk ordinals preserved.
- Standalone runs unchanged.

## Governance changes requiring human acceptance

- Narrow scheduler exception allowing one unsigned local commit per accepted non-final sequence
  phase via `write-tree` / `commit-tree` / `update-ref` CAS only.
- Updated `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc` and
  `ai-dev-loop-loop-and-resume-contracts.mdc`.
- Operator documentation in `docs/index.md`, `docs/guia/flujo-completo.md`,
  `docs/guia/guia-rapida.md`, `docs/operacion/seguridad-privacidad.md`, and
  `docs/referencia/cli.md`.

## Demonstrated recovery and abort behavior

- Orphan intent idempotent replay after intent/trusted-tree/evidence persistence before SQLite
  CAS; crash-gap reconstruction after intent-only persistence without regenerating intent JSON.
- Orphan intent promotion refuses tampered `commit_message`, successor run/ordinal, and `branch_ref`
  before Git mutation or successor materialization.
- Already-applied checkpoint adoption without a second commit when trusted tree, evidence, and
  commit identity match the immutable intent.
- Pre-CAS abort/lease fencing prevents `commit-tree` mutation; `checkpoint_cas_authorized`
  uncertainty is recorded before `update-ref`, and a post-authorization abort re-check blocks
  `update-ref` when abort wins after authorization. Pre-CAS holds (`ref_may_have_advanced=0`)
  still allow operator abort and release reservation. Commit-object evidence alone does not
  defer terminal abort.
- `checkpoint_cas_authorized` holds defer terminal abort and preserve reservation ownership until
  live `HEAD` reconciliation or explicit adoption; they do not authorize initiating another CAS
  after abort.
- Post-CAS holds (`ref_may_have_advanced=1`) are upgraded immediately after a successful
  `update-ref` CAS via an in-process callback, before postcondition validation; exception paths
  infer ref advancement from live `HEAD` and retain `checkpoint_cas_authorized` uncertainty on
  ambiguous subprocess outcomes rather than the caller's local `ref_updated` flag.
- `SequenceHandoffService._reconcile_checkpoint` coordination tests cover abort after CAS
  authorization, abort before authorization, crash after successful CAS before
  `on_ref_advanced`, ambiguous `update-ref` failure, and ambiguous recovery `HEAD` reads that
  preserve `checkpoint_cas_authorized` deferral through `default_abort_service` before exact
  adoption without a second `update-ref` through the production handoff service path.
- Restart recovery upgrades existing false holds when live `HEAD` already equals the persisted
  checkpoint commit, not only when no hold row exists.
- `inspect_checkpoint_ref_advancement()` distinguishes verified applied, verified non-applied at
  parent, ref elsewhere, and ambiguous failed reads. `_reconcile_checkpoint_hold_from_evidence()`
  preserves existing `checkpoint_cas_authorized` holds unless live `HEAD` verification upgrades
  them to `checkpoint_ref_advanced` or a fenced recovery tick resolves an acknowledged abort with
  verified non-applied repository state; failed reads no longer replace authorization uncertainty
  with `checkpoint_commit_evidence_recovered`.
- Acknowledged abort after CAS authorization resolves on a subsequent recovery tick once fencing
  proves no further Git mutation is possible and the repository still matches the trusted staged
  checkpoint inputs; the hold is released and the same tick durably completes the recorded abort
  (`RunAbortedEvent`, reservation release, and cleanup) without a second user abort request.
  Pre-CAS acknowledged aborts with no CAS uncertainty complete on the next reconciliation tick
  through the same shared completion path.
- Acknowledged abort blocks further pre-CAS mutation but allows post-CAS reconciliation only when
  the durable hold records `ref_may_have_advanced=1` (live HEAD at checkpoint commit), not from
  commit-object persistence alone.
- `default_abort_service` and CLI `scheduler abort` always attach the default artifact root so
  checkpoint abort coordination can inspect durable evidence.
- Crash recovery infers `ref_may_have_advanced` from live `HEAD` versus persisted commit evidence,
  not from `result.json` presence.
- Authenticated predecessor `result.json` is required for ordinal 2+ parent selection; the
  selected commit is verified with `git cat-file` identity against frozen intent, trusted tree,
  and ledger bindings. Tampering `commit_sha256` to an unrelated repository commit fails closed
  without Git mutation or successor materialization.
- Parent HEAD drift refusal when an unrelated commit advances HEAD while staged patch bytes
  remain unchanged.
- Read-only inspection of historical schema v4/v5/v6 databases without migration; aborted-run
  safe-action queries skip v7-only checkpoint-hold tables on pre-v7 schemas.
- Post-CAS staged drift refusal blocks successor authorization.
- Evidence tree/commit tampering with unchanged trusted-tree hash is refused via trusted-tree
  authentication and `git cat-file` identity verification.
- Lease expiry between validation and later Git subprocesses prevents continued mutation under a
  fresh lease.

## Validation commands

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_3_checkpoint_state.py \
  tests/unit/scheduler/test_phase20_3_git_checkpoint.py \
  tests/unit/scheduler/test_phase20_3_handoff.py \
  tests/unit/scheduler/test_phase20_3_handoff_recovery.py \
  tests/unit/scheduler/test_phase20_3_checkpoint_concurrency.py \
  tests/unit/scheduler/test_schema_readonly_historical.py \
  tests/unit/scheduler/test_tick.py \
  tests/unit/scheduler/test_phase20_2_sequence_start.py \
  tests/integration/test_phase20_3_sequence_handoff.py \
  tests/integration/test_phase20_2_sequence_start.py
```

```bash
uv run ruff format --check src tests
uv run ruff check src tests
uv run mypy src
uv run mkdocs build --strict
```

## Validation results (2026-09-13, correction round 10)

- `101 passed` in the focused Phase 20.3 and scheduler regression set above (`TMPDIR=/tmp`) after
  deferred-abort completion on recovery ticks; production concurrency tests issue `abort_run`
  once and drive ordinary recovery ticks through terminal `AbortedState`, unchanged HEAD/staged
  patch, no `update-ref`, no successor, and idempotent subsequent ticks plus abort inspection.
- `ruff format --check`, `ruff check`, `mypy src`, and `mkdocs build --strict` required after
  `complete_recorded_checkpoint_abort_if_ready` and handoff reconciliation updates.
- Disposable-repository tests only; no Gate B real-workstation validation.

## Residual risks

- Git ref mutation and SQLite handoff remain separate transactions; reconciliation depends on
  immutable intent, immutable trusted reviewed tree, authenticated evidence, durable checkpoint
  holds, mutation fencing, and idempotent ledger replay.
- Gate B real-workstation validation with user signing keys and production hook environments
  remains for controlled acceptance outside disposable-repository tests.

## Next phase boundary

Phase 20.4 owns sequence lifecycle presentation, retry/recover UX, and final reporting polish.
Recovery tests, abort fencing, and narrow authority documentation are implemented in Phase 20.3.
