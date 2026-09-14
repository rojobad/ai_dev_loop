# Phase 20.2 — Lazy First-Phase Materialization and Authorization

## Goal

Make `ai_dev_loop scheduler sequence start <sequence-id>` the single explicit
operator authorization for a prepared sequence and materialize only its first
phase as a normal scheduler run.

The first run must be constructed exclusively from the immutable Phase 20.1
sequence entry, receive that entry's preassigned run ID, claim the normal
run-owned repository reservation, and enter the existing `authorized` boundary
without launching Git probes, Cursor, or Codex inside the start command. The
normal scheduler tick then performs admission and the existing run loop.

This phase deliberately stops after first-run materialization. It does not
create checkpoint commits or materialize later phases; those behaviors belong
to Phase 20.3.

## Non-Goals

- Do not create a run for phase 2 or any later entry.
- Do not commit, invoke Git hooks or signing, transfer reservations, advance a
  completed phase, or implement checkpoint recovery.
- Do not make `sequence start` reread live plan, prompt, manifest, YAML defaults,
  executable resolution, or model settings as execution authority.
- Do not add automatic sequence retry, phase skipping, reordering, insertion,
  deletion, branching, final commit, push, PR, or merge behavior.
- Do not change the lifecycle or identity rules within an individual Cursor/
  Codex run.
- Do not change Phase 19 `waiting_codex_capacity`: it remains a non-terminal
  run wait that retains the reservation and resumes the exact reviewer B.
- Do not implement the complete sequence abort/reporting/documentation surface
  assigned to Phase 20.4.

## Scope

- Add a typed immutable sequence binding to newly materialized scheduler runs:
  sequence ID, one-based ordinal, total phases, and frozen entry hash.
- Add the next compatible submitted-context/schema version while retaining all
  historical context versions unchanged.
- Add an internal sequence run materializer that verifies Phase 20.1 artifacts,
  creates/verifies the ordinary run artifact tree, inserts the preassigned run,
  acquires its repository reservation, records normal submission and
  authorization events, and advances sequence state in a fenced operation.
- Implement public `scheduler sequence start <sequence-id>` with stable text and
  JSON results, idempotent replay, conflict handling, and safe next actions.
- Expose safe sequence provenance in individual run status and the active run in
  exact sequence status without leaking protected inputs.
- Allow the existing scheduler tick to process the authorized first run without
  sequence-specific agent execution code.
- Provide an honest temporary Phase 20.2 boundary after the first run finishes:
  no commit and no later run materialization are claimed until Phase 20.3.
- Add tests, minimal current-behavior documentation, and a Phase 20.2 findings
  artifact.

## Out of Scope

- A commit-capable Git adapter, changes to the categorical no-commit Cursor
  governance rules, Git reference updates, and checkpoint intent artifacts.
- Automatic handling of `completed` or `completed_with_residual_risk` for
  non-final phases beyond exposing the honest Phase 20.2 stop condition.
- Sequence-level abort process control, terminal failure projection, rich final
  report, sequence-wide history/list, or generic retry/recovery commands.
- Target `ai_dev_loop.yaml`, package-owned skills, global integrations, timer
  installation/enablement, Codex account mutation, or real model execution.
- Materializing phases from changed repository files or from ad hoc CLI input.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file.
- Phase 20.1 plan, findings, migration, schemas, models, sequence artifact
  layout, prepare/status services, tests, and implemented CLI contract.
- The final Phase 19 findings and implementation, especially
  `waiting_codex_capacity`, tick eligibility, reservation retention, exact
  reviewer identity, and abort precedence.
- Current Phase 17.1–17.6 and Phase 18 plans/findings for submit, start, Git
  admission, effects/attempts, review completion, reservations, status, privacy,
  abort, and controller-optional operation.
- `src/ai_dev_loop/cli.py` and `src/ai_dev_loop/commands/scheduler.py`.
- `scheduler/application/submission.py`, `start.py`, `status.py`,
  `safe_actions.py`, `contracts.py`, `tick.py`, `scheduler_preflight.py`, and
  `run_state_bridge.py`.
- `scheduler/domain/state.py`, `events.py`, `reducer.py`, `common.py`, and the
  Phase 20.1 sequence domain models.
- `scheduler/infrastructure/sqlite_store.py`, migrations, protected artifact
  and path helpers, and repository target/admission adapters.
- Unit/integration tests for submission, start, status, store, schemas, tick,
  reservation conflicts, Phase 19 capacity waiting, and Phase 20.1 prepare.
- Current CLI, workflow, observability, privacy, and troubleshooting docs.

Phase 20.1 must be independently reviewed, committed, and present as the clean
baseline before Phase 20.2 begins.

## Cursor Rules And Skills

- Follow `AGENTS.md` and every repository-local Cursor rule listed in Phase
  20.1, especially state/schema compatibility, immutable artifacts, reservation
  ownership, exact agent identity, bounded effects, privacy, and phase honesty.
- The repository has no `.cursor/skills` directory. Do not modify or invoke the
  package-owned `ai-dev-loop-controller` or `ai-dev-loop-handoff` assets.
- Use only temporary repositories and XDG/config homes, fake Cursor/Codex
  executables, injected clocks/IDs, and injectable stores/ports in tests. Do not
  invoke real model activity, systemd, credentials, hooks, or global installs.
- Keep implementation changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### One authorization, one materialized run

- `sequence start` is the single explicit durable authorization for the whole
  frozen sequence, including the future intermediate-checkpoint policy already
  present in its immutable definition. Phase 20.2 exercises only authorization
  and first-run materialization; it must not perform later-phase work.
- A prepared sequence has zero scheduler runs. A successfully started Phase
  20.2 sequence has exactly one materialized run: ordinal 1 with the exact
  preassigned `planned_run_id`.
- Never expose a public way to start a planned phase directly. Standalone
  `scheduler start <run-id>` may idempotently observe the already-authorized
  materialized run, but it must not materialize a missing planned run or bypass
  sequence order.
- Replaying `sequence start` must return the same run and current safe state. It
  must never create a second run, reservation, submission event, authorization
  event, or artifact set.

### Frozen-entry materialization

- Build the new run context exclusively from verified Phase 20.1 protected
  artifacts and fully resolved entry payload. Do not call the public stdin
  reader, reread repository YAML for defaults, resolve a different executable,
  or accept per-start overrides.
- Repository plan and prompt source paths remain frozen repository-relative
  bindings. Existing first-tick preflight must still require the repository plan
  hash and any present prompt-source hash to match the frozen snapshots.
- Copy bytes into the deterministic run artifact layout through bounded
  write-or-verify operations; never symlink run artifacts to sequence artifacts.
  A run remains independently auditable after materialization.
- Add a typed `SequenceRunBinding` or equivalent to the next submitted-context
  version. It contains only sequence ID, ordinal, total, and entry hash; it does
  not contain mutable sequence state, successor IDs, commit SHA, or raw inputs.
- Include sequence binding in run submission identity. Standalone contexts and
  all prior schema versions remain unchanged and readable.

### Transaction, reservation, and phase state

- Before mutation, verify the sequence and entry payload/artifact hashes and
  ensure the sequence is exactly `prepared` with no materialized entry.
- The materialization commit boundary must atomically establish, in the central
  ledger: the run row, normal run-submitted event, active reservation owned by
  that run, normal run-authorized event/state, sequence-start authorization,
  current ordinal/run projection, and entry materialized status.
- Reuse extracted submission/start domain and store primitives where safe, but
  do not recursively invoke the CLI or emulate stdin. The special path may
  consume only the prevalidated frozen entry.
- A reservation conflict leaves the sequence prepared and materializes no run.
  Never release, steal, overwrite, or wait behind a reservation owned by an
  unrelated run.
- `sequence start` itself remains process-free and Git-command-free. Actual
  branch, HEAD, repository identity, and clean-worktree admission are captured
  once by the existing first scheduler tick after authorization.
- If admission blocks, the sequence must expose the active run's safe stop; it
  must not materialize a successor or try another baseline.

### Artifact/database crash safety

- Filesystem and SQLite are not one transaction. Use deterministic paths,
  preassigned IDs, write-or-verify bytes, version/CAS checks, and replayable
  intent so a crash cannot produce two first runs or change frozen content.
- Orphaned deterministic run artifacts for a planned-but-not-yet-inserted run
  may be adopted only when every protected hash matches its sequence entry.
  Conflicting or unexpected artifacts block materialization and remain for
  diagnosis.
- A database run without its complete required artifact set is corruption and
  must not be started by tick. Preserve existing preflight fail-closed behavior.

### Existing run and capacity behavior

- Once authorized, ordinal 1 follows the ordinary scheduler state machine,
  global tick lease, repository admission, effects, attempts, capacity claim,
  Cursor chat, reviewer B binding, bounded corrections, usage waits, and abort
  checks. Do not fork a sequence-specific agent loop.
- `waiting_codex_capacity` remains active indefinitely across ticks, does not
  consume a review iteration, retains the first run's reservation, and resumes
  only the exact bound reviewer when Phase 19 proves capacity.
- Standalone run submission/start/status and worktree-conflict behavior must
  remain unchanged.

### Honest Phase 20.2 boundary and privacy

- Sequence status must expose only safe identifiers, ordinal/count, phase name,
  run state, residual-risk flag when known, timestamps, and a supported next
  action. Do not expose prompts, plans, configs, full agent IDs, artifacts, or
  raw output.
- Until Phase 20.3 exists, a successful first non-final phase must not claim to
  have committed or advanced. Surface a clear non-automatic checkpoint boundary
  and preserve the run's existing terminal/staged result.
- Do not document end-to-end sequence execution as available in this phase.

## Implementation Plan

1. Characterize Phase 20.1 prepare/status persistence and existing standalone
   submission/start transaction behavior. Extract the smallest reusable
   frozen-context insertion and authorization primitives without changing
   standalone results.
2. Add sequence provenance to the next current submitted-context model/schema,
   state validation, idempotency payload, run status summary, and fixtures while
   proving every historical context version still validates.
3. Implement deterministic run-artifact materialization from a verified frozen
   sequence entry. Add content-conflict and interruption injection tests before
   wiring public start.
4. Implement the ledger transaction that starts a prepared sequence, inserts
   and authorizes ordinal 1, claims the ordinary run-owned reservation, and
   advances entry/sequence projections with strict version checks.
5. Add `scheduler sequence start` CLI/rendering plus idempotent replay and exact
   conflict/not-found/not-prepared behavior. Make safe actions truthful at the
   Phase 20.2 checkpoint.
6. Prove the existing tick admits and executes the materialized run using the
   normal workflow, including Phase 19 capacity waiting, without creating any
   later phase.
7. Update only implemented-behavior docs and write
   `archive/implementation-history/findings/phase-20-2-lazy-phase-materialization.md`.

## Testing Criteria

- **Context/schema tests:** valid sequence-bound current context; invalid zero,
  out-of-range, total/ordinal, sequence ID, planned-run ID, and entry-hash
  combinations; historical standalone contexts/snapshots continue loading.
- **Materializer unit tests:** exact bytes/config/bindings copied from sequence
  artifacts; no live YAML/prompt/executable reread; no symlinks; bounded writes;
  conflicting/partial artifacts block; identical retry verifies and reuses.
- **Start/store transaction tests:** prepared-to-active transition, preassigned
  run ID, two ordered run events, authorized state/version, active reservation
  owned by ordinal 1, entry projection, sequence authorization timestamp, CAS
  loss rollback, foreign keys, and no half-inserted rows.
- **Idempotency/concurrency tests:** repeated and concurrent sequence start create
  one run/reservation/event chain; unrelated reservation conflict leaves the
  sequence prepared; standalone submit cannot claim the active worktree; a
  planned run ID cannot be addressed before materialization.
- **No-process tests:** sequence start invokes no Git, Cursor, Codex, systemd,
  hook, or model subprocess and performs no target-worktree mutation.
- **Integration tests:** first tick performs normal clean admission, phase 1 can
  traverse the existing fake Cursor/Codex loop, `waiting_codex_capacity` holds
  the same run/reservation and resumes it, admission failure does not create
  phase 2, and successful completion still creates no commit or second run in
  Phase 20.2.
- **Regression/privacy tests:** standalone submit/start/status/list/tick/abort
  behavior and Phase 19 tests remain green; sequence/run output contains no
  frozen prompt/config/plan, raw output, full reviewer ID, or sensitive path.

## Validation

Run focused tests, scheduler regressions, and static checks. At minimum:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler/test_phase20_1_sequence_prepare.py \
  tests/unit/scheduler/test_phase20_2_sequence_start.py \
  tests/unit/scheduler/test_start.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_sqlite_store.py \
  tests/integration/test_phase20_2_sequence_start.py \
  tests/integration/test_phase17_1_submit.py \
  tests/integration/test_phase17_2_tick_control.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/scheduler tests/integration
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

Use fakes only. Do not run real agents, real account probes, real systemd, hooks,
or workstation mutations.

## Risks Or Recovery Notes

Phase 20.2 makes sequence authorization durable before the future checkpoint
engine exists. Its public/docs boundary must therefore be explicit: only the
first run is automatic in this phase. A test or operator run that reaches a
successful terminal state remains staged and requires manual handling; no later
entry may appear.

The most important corruption risk is divergence between a frozen entry, its
deterministic run artifact copy, and the inserted run context. Verify all three
hash sets and fail closed instead of trying to reconstruct missing content from
the live repository.

## OpenQuestions

None.
