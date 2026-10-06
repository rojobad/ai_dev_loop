# Phase 23.4 — Cursor recovery within the original sequence

## Goals

Extend accepted manual recovery to an eligible blocked sequence leaf, initial or
correction. Atomically adopt one successor at the same sequence/ordinal, then
resume ordinary Cursor, staged review, checkpoint and later-phase/finalization
behavior. Safely converge publication and cancellation through real restart paths.

## Non-Goals

- Construct another sequence, infer phase identities from names or reorder phases.
- Accept a phase through a retry command or change authorized checkpoint commits.
- Weaken standalone, reviewer, budget, process or artifact integrity contracts.

## Scope

Typed Cursor sequence replacement intent; complete lineage v2 models/schemas;
ledger/store CAS and reservation adoption; dispatch/adoption barrier; sequence
restart/abort integration; correct projections, tests and operational docs.
Reuse accepted standalone recovery evidence, records, budgets and envelopes.

## Out of Scope

No provider/automatic quota retry policy, timer changes, installation, remote
control endpoints, hub implementation, live sequence recovery or edits to YAML
and planning/review skills. Preserve old review replacement intent semantics.

## Required Context

Read [the overview](phase-23-replanned-overview.md) and the original
[23.1](phase-23-1-cursor-recovery-evidence.md),
[revised 23.2](phase-23-2-initial-standalone-cursor-recovery.md),
[23.3](phase-23-3-cursor-correction-continuity.md), `AGENTS.md`, current CLI,
lineage, state, recovery and privacy docs, and
[the revised handoff](phase-23-1-to-23-2-handoff.md). The preserved 23.1 commit is
not independently accepted; revised 23.2 acceptance includes E-01–E-04 and I-00
closure. Verify accepted revised 23.2 and 23.3 in the checkout, and that standalone
initial/correction recovery has complete authenticated publication, v1/v2
compatibility, exact-chat/B/budget continuity, abort/restart convergence and a
real runnable effect. These are accepted dependencies, not assumed WIP.

Baseline production paths under `src/ai_dev_loop/scheduler/`:
`application/sequence_review_recovery.py`, `sequence_reconcile.py`,
`sequence_restart_reconcile.py`, `sequence_handoff.py`, `git_checkpoint.py`,
`sequence_checkpoint_evidence.py`, `sequence_abort.py`,
`sequence_lineage_ops.py`, `sequence_lineage_projection.py`, `sequence_status.py`,
`sequence_report.py`, `tick.py`; domain `sequence.py`, `sequence_run_lineage.py`,
`sequence_execution_replacement.py`; infrastructure `sequence_run_lineage_store.py`
and `sqlite_store.py`. Inspect actual callers and schema resources before work.
Existing review-replacement intent requires B/staged patch and is not a valid
Cursor intent before the first review. Existing lineage/store/checkpoint rules
assume generation >= 2 is `same_reviewer_retry`; all affected callers need updates.
Public `integration_api/models.py` and the corresponding sequence-shared schema
already represent `attemptKind` as an open string; retain the API major.

## Cursor Rules And Skills

Follow `AGENTS.md` and these files under `.cursor/rules/`:
`ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
`ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`,
`ai-dev-loop-codex-review-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`,
`ai-dev-loop-docs-acceptance-contracts.mdc`, and
`ai-dev-loop-global-integrations-contracts.mdc`.
There is no `.cursor/skills/`. Keep
`.agents/skills/create-cursor-plan/SKILL.md` and
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md` unchanged; the latter
governs the independent read-only reviewer. Update only affected runtime rules
and AGENTS statements to reflect actual accepted sequence recovery.

## Architecture Guardrails

- Exact sequence ID, frozen entry hash, ordinal, current leaf/generation and
  authoritative relation determine adoption. Never infer lineage from display
  names, run ID prefixes or merely the existence of a successor directory.
- Reuse predecessor recovery authority and publication mechanics. Sequence
  adoption is an additional mandatory dispatch barrier, not a best-effort update.
- Keep source blocked and prior attempt records immutable. A replacement is a
  new generation of the same phase, not completion or another planned phase.
- Preserve repository ownership during uncertain publication/CAS/abort. No
  in-flight outcome is assumed successful; converge after it becomes known.
- Accepted non-final review alone authorizes existing checkpoint commit behavior;
  final/standalone output remains staged. Retry itself makes no Git mutation.

## Implementation Plan

**S-01 — Complete versioned sequence provenance.** Add a distinct v1 typed Cursor
sequence replacement intent binding sequence, source/successor, ordinal,
contiguous generations, frozen entry, recovery key/record digest, worktree and
publication/adoption authority. Do not fabricate staged patch or reviewer for an
initial failure; correction uses accepted 23.3 evidence without changing the
existing review-intent v1/v2 requirement set. Support historical review intents
through their existing readers and publication path.

Add `cursor_retry` to a complete v2 attempt, phase-execution and aggregate-lineage
schema/model set. Preserve generation 1 `planned_run`, source-required generation
>= 2, exact immediate predecessor, unique IDs, contiguous chain, current-leaf
acceptance and terminal-resolution invariants. New writers use v2; retain genuine
v1 inputs and historical numeric behavior under their original readers. Do not
validate new Cursor lineage as old `same_reviewer_retry`, return early to bypass
aggregate schema validation, or patch only the nested attempt schema. Define
required null/absent rules consistently; new strict integer fields reject bools
and strings. Align migrations, readers/writers, stores, status/report/checkpoint
callers and open-string Integration API projections. Inspection never migrates.

**S-02 — Idempotent fenced replacement and adoption.** On a fresh forced sequence
recovery reauthenticate evidence and blocked sequence leaf, sequence/entry hash,
ordinal/generation, idle process/hold state, reservation and absence of cancellation.
Reuse one source/failure relation. Authenticate and return an existing relation
before requiring the original source to still be current/blocked; it may already
have been replaced and the sequence may have advanced. A new replacement of a
stale source must still reject. Different recovery/review/abort callers cannot
claim the same authoritative leaf inconsistently.

Durably publish the candidate and its private inputs under accepted pending
publication guards. Atomically CAS sequence leaf/lineage, current run, reservation,
active sequence state and intent adoption. The ready postcondition requires both
authenticated recovery inputs and authoritative sequence adoption with exactly
one retry effect for the current successor. No candidate may dispatch before
that postcondition, including on restart, direct run tick or queued old effects.
Initial recovery carries no invented B; correction carries exact B/counters/
ceiling through accepted 23.3. Do not edit frozen sequence entries or future IDs.

**S-03 — Restart, abort and eventual convergence.** Wire the new intent into
actual sequence restart reconciliation and tick publication/reconciliation callers.
An interruption around file publication, candidate creation, adoption CAS or
effect readiness retains a durable owned pending intent. Repeated observation
either proves adoption/ready once or resolves cancellation; never leaks the
reservation or leaves a permanently non-dispatchable adopted successor after
uncertainty resolves. Sequence abort and candidate/run abort persist cancellation
before new effects; cancel/fence pending replacement and prevent future phases.
Preserve unresolved process/checkpoint holds and reconcile through established
abort semantics. Do not undo an observed checkpoint/ref CAS or invent success.

**S-04 — Ordinary phase completion and honest projections.** After retry success,
use normal staging, initial B bootstrap or correction B resume, review decisions
and budget accounting. Accepted non-final phase uses the existing checkpoint
intent/commit/adoption flow to materialize the next original entry. Final phase
reaches ordinary finalization without a new final commit. Update any review-only
kind guards in real stores/checkpoint callers; status-only recognition is
insufficient. Reports/status/API show `cursor_retry`, exact accepted successor,
generation and phase lineage without sensitive content. `--check`/`--force` help
and current Spanish CLI, lineage, state, recovery, troubleshooting and privacy
docs explain native continuation, repeat behavior and all rejection cases.

## Testing Criteria

Mandatory unit/schema and end-to-end production CLI/service/tick tests, suggested
`tests/unit/scheduler/test_phase23_4_sequence_recovery.py`,
`test_phase23_4_sequence_publication.py`,
`tests/integration/test_phase23_4_sequence_recovery.py`, and authentic v1 lineage/
review-intent fixtures under `tests/fixtures/phase23_4_historical/` captured from
baseline writers with commit provenance. Use fake agents, injected time, isolated
native-WSL HOME/XDG/repos and deterministic race synchronization.

| Contract | Independent observable acceptance |
| --- | --- |
| S-01 | Validate full v1/v2 documents with matching domain models/schemas and independent invariant checks. New cursor attempts cannot validate as v1. Historical planned/review-retry lineages and review intents still load unchanged; malformed aggregate chain, required/null/numeric values reject. Reopen/migration/projection round trips preserve meaning without read-only migration. |
| S-02 | Real middle phase of a three-phase fake sequence fails initial Cursor; force creates/adopts one successor at the same ordinal. Direct tick cannot launch before adoption. Race duplicate force, reviewer replacement and tick: one authoritative leaf/reservation/dispatch. After acceptance/next-phase advancement, duplicate original-source command returns the existing relation. Fresh stale-leaf recovery rejects. |
| S-03 | Crash at intent, private input, candidate, adoption and ready-effect boundaries, then actual restart/tick callers converge. Holds retained while uncertain, eventually ready once or fully cancelled. Deterministic sequence-abort/run-abort races before/during/after adoption prevent late launch/future materialization, preserve edits and reconcile/release only when safe. |
| S-04 | Complete the recovered middle phase through normal review, exactly one authorized checkpoint, next original phase and finalization. Same sequence ID/order/entry hash, source still blocked, accepted run is successor and kind is cursor_retry. Repeat with failed correction, exact B and an inherited extended ceiling; multiple retry generations preserve one note. Standalone and existing review-recovery/checkpoint paths still work. |

Verify index/diff/untracked bytes unchanged by force itself, ordinary stage-all
after Cursor success, and no unauthorized commits. API/status/report summaries
must remain private. Helpers must not supply production adoption, CAS locks,
artifact copies or state changes. Tests must prove completion, not accept a
blocked sequence as evidence of a successful recovery.

## Validation

Run the selected subphase-contract tests and affected regressions below during
initial implementation and first review. After each correction, rerun the failed
and affected tests; expand the selection only for a concrete remaining risk.
Do not run the repository-wide pytest suite during implementation or review.
That suite is a separate pipeline/integration gate after the sequence, described
in the overview. All mandatory behavioral coverage remains required; a pending
pipeline gate alone does not block automated acceptance of this subphase.
For referenced historical plans, inherit behavioral contracts, not their generic
repository-wide validation command. Report exact selections and results.

```bash
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23_4*.py tests/integration/test_phase23_4*.py
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23_1*.py tests/unit/scheduler/test_phase23_2*.py tests/unit/scheduler/test_phase23_3*.py tests/integration/test_phase23_1*.py tests/integration/test_phase23_2*.py tests/integration/test_phase23_3*.py tests/unit/scheduler/test_phase20_7_sequence_run_lineage.py tests/integration/test_phase20_8_sequence_review_retry.py tests/unit/scheduler/test_phase20_8_sequence_review_retry_concurrency.py tests/unit/scheduler/test_phase20_9_review_retry_barrier.py tests/integration/test_phase22_sequence_routing.py tests/unit/integration_api/test_contract.py
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Use isolated Python 3.11+ `.venv`/`uv`, verify local CLI help and report S-01–S-04
implementation, actual callers, tests and exact executed/failed/unexecuted checks.
Missing aggregate validation, adoption barrier, abort convergence or required
test/doc coverage is incomplete delivery. Final operator acceptance can inspect
the fake three-phase recovery evidence. Installation and real hub sequence
recovery remain separate explicitly authorized actions.

## Risks Or Recovery Notes

Sequence publication and restart wiring remained P1 issues in the combined run.
This phase owns them end to end, reusing already accepted evidence and standalone
publication instead of introducing all concerns at once. The first three phases
cannot mutate a sequence; accepted 23.4 is required for the original hub use case.
No plan creates a live sequence or authorizes recovery of aborted runs.

## OpenQuestions

None. Verify accepted revised 23.2 (including the 23.1 closure) and 23.3 before
implementing sequence adoption.
