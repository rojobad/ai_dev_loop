# Phase 23.2 — Manual recovery of an initial standalone Cursor turn

## Goals

Enable `ai_dev_loop scheduler cursor-retry RUN_ID --force --output json` for an
eligible failed initial standalone turn. Publish one successor, resume the same
chat with the exact failed prompt plus one operational note, preserve partial
work, and complete the normal staging/first-review flow. Make replay, restart and
abort safe within this complete supported slice.

## Non-Goals

- Recover a correction or sequence run; those are explicit later capabilities.
- Reset a chat, review budget, frozen configuration or implementation baseline.
- Automatically retry generic failures or classify/repair provider quota.

## Scope

Forced initial standalone authorization; versioned private recovery authority;
durable publication and retry effect; exact prompt binding/runner validation;
restart/abort integration; repeat recovery of failed initial successors; receipts,
tests and current operational documentation.

## Out of Scope

No sequence adoption/lineage migration, correction recovery, reviewer switching,
quota policy, timers, installation, live recovery or changes to the hub repo.
Do not edit `ai_dev_loop.yaml`, planning/review skills or the aborted worktree.

## Required Context

Read [the overview](phase-23-replanned-overview.md),
[Phase 23.1](phase-23-1-cursor-recovery-evidence.md), `AGENTS.md` and relevant
current CLI/state/privacy/recovery docs. Before dependent implementation, verify
23.1 is accepted in the actual checkout: its production `--check` path and
analyzer must authenticate the baseline initial failure without mutation.
If absent, stop dependent work; the aborted combined patch does not satisfy it.

Existing paths under `src/ai_dev_loop/`: scheduler `application/cursor_evidence.py`,
`attempt_service.py`, `cursor_workflow_service.py`, `review_recovery.py`, `tick.py`,
`tick_fencing.py`, `abort.py`; `cursor_attempt_runner.py`; domain `state.py`,
`events.py`, `effects.py`; infrastructure `sqlite_store.py`, protected artifact
and reservation/process adapters. Recheck actual names/callers, particularly
terminal failure releasing its reservation and normal B bootstrap after success.
Existing review recovery is a publication reference, not eligibility for this
failed unstaged initial turn. Read existing timeout and abort integration tests.

## Cursor Rules And Skills

Follow `AGENTS.md` and these files under `.cursor/rules/`:
`ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
`ai-dev-loop-state-and-schema-contracts.mdc`,
`ai-dev-loop-loop-and-resume-contracts.mdc`,
`ai-dev-loop-codex-review-contracts.mdc`, `ai-dev-loop-abort-contracts.mdc`,
`ai-dev-loop-docs-acceptance-contracts.mdc`, and
`ai-dev-loop-global-integrations-contracts.mdc`.
There is no `.cursor/skills/`. Follow the authoring contract from
`.agents/skills/create-cursor-plan/SKILL.md` and the separate read-only reviewer
role in `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; do not edit
those skills. Update only affected runtime rules/AGENTS statements for this slice.

## Architecture Guardrails

- Reauthenticate the Phase 23.1 evidence inside the new durable authorization
  boundary. A saved inspection receipt is not authority.
- Reject correction and sequence sources with clear unsupported-scope reasons,
  including sequence-linked runs addressed by ID outside sequence commands.
- Keep the blocked source/journal/artifacts unchanged; no clean-worktree
  re-admission, `git add`, rollback or branch/HEAD mutation during authorization.
- Protect exact identity, input hashes, typed publication ownership and attempt
  fences. A pending candidate may not dispatch through any runner/tick caller.
- Preserve frozen config and zero completed reviews for this supported initial
  slice. A source with prior B/reviews requires 23.3; never pretend B is absent.
- Default output contains safe provenance/reasons, not prompts or full chat IDs.

## Implementation Plan

**I-01 — Scoped CLI and stable relation.** Add `--force`, mutually exclusive with
`--check`; keep no-flag timeout behavior and its output unchanged. Eligible means
Phase 23.1 authenticated `blocked(cursor_failure)`, initial standalone turn,
genuinely no reviewer/prior completed review and confirmed terminated ownership,
with no cancellation/checkpoint hold or conflicting worktree owner. Bind a unique
recovery key to source run + decisive failed attempt, independent of timestamps.
Look up/authenticate an existing published or pending relation before considering
new creation; repeated/concurrent commands return the same successor/intent even
after that successor advances. Reject tampered replay authority, not merely
duplicate insertion. Retrying a newly failed initial successor uses its own
failure key and authenticated parent provenance.

**I-02 — Exact private envelope and v1 evidence.** Base bytes are the exact UTF-8
prompt artifact sent to the failed invocation, including any existing continuation
envelope. Append two LF bytes, the following exact note, and one final LF:

```text
Recovery note: A previous attempt of this turn was interrupted and may have left partial work in this repository. Inspect the existing changes and continue according to the original plan and instructions. Preserve the work already present; determine what is complete and what still needs implementation or verification. Do not reset or discard existing work merely to start over.
```

For another failed manual recovery, authenticate its v1 record and reuse the
original base plus one note. Do not identify an envelope by text matching.
Define complete v1 domain/schema models for initial Cursor recovery record and
publication intent. Bind source/successor, causal attempt/dispatch, exact chat,
logical iteration, admitted/frozen input evidence, base/effective prompt paths and
hashes, and parent provenance when present. The supported turn kind is explicitly
`initial`, B is explicitly not created, completed reviews are zero. Required
fields cannot be absent/null; parent source reference uses required null for no
parent. Versions/iterations are strict JSON integers, not strings/booleans.
Hash actual canonical published bytes; persist and verify the record's own
artifact path/digest through the ledger relation. A prompt hash cannot stand in
for the recovery record hash. Retain all original artifacts and ownership IDs.

**I-03 — Publication, ownership and executable postcondition.** Persist a unique
publication intent and non-dispatchable candidate before external artifact
publication, using an existing workflow state plus a typed pending-publication
guard. The guard is checked by tick selection and all attempt dispatch paths;
do not rely on CLI ordering. Acquire/transfer the available reservation to the
candidate under that durable hold; refuse unrelated owners. Publish authenticated
copied inputs, restored admitted checkpoint and effective prompt privately,
outside long database transactions. Only the final guarded transaction marks
publication ready and installs exactly one fenced `cursor.run_turn` effect for
the restored iteration. No admission/create-chat effect is installed.

Precondition: current causal evidence, idle ownership, no abort. Authority:
pending intent, candidate/relation, reservation and artifact digests. Effect:
bounded private publication followed by one retry dispatch. Postcondition:
authenticated inputs + ready relation + runnable retry effect, or an owned
non-dispatchable pending intent. Reconcile intent/candidate/files/ready publication
after interruptions through actual scheduler tick/restart callers. Retain holds
while uncertain; converge to ready or cancelled after observations resolve.
Abort of the candidate persists intent, prevents dispatch and converges cleanup
through existing abort/tick paths. Do not mutate an immutable blocked source to
simulate cancellation. Preserve its files, index and all private attempt output.

**I-04 — Resume and normal first review.** Bind effective prompt/chat and source
provenance consistently in `attempt_service`, evidence validators and runner;
resume the exact chat. Authentication must still pass from the current successor
invocation into outcome ingestion. After success normal staging creates the
reviewed snapshot and normal Codex bootstrap creates B once. Subsequent ordinary
review/fix behavior remains supported, although forcing a failed correction is
not yet available. Initial/standalone final success leaves changes staged.
Result includes source/successor, changed/reused status and supported next action;
pending publication never advertises ready agent execution. `--check` reports
mutation supported only for this accepted slice. Update CLI, state-artifact,
recovery, troubleshooting and privacy docs with these exact boundaries.

## Testing Criteria

Mandatory new unit/schema, CLI integration and deterministic interruption/race
tests, suggested `tests/unit/scheduler/test_phase23_2_initial_recovery.py`,
`test_phase23_2_publication.py`,
`tests/integration/test_phase23_2_initial_recovery.py` and v1 fixtures under
`tests/fixtures/phase23_2_recovery/`. Use fake CLIs/backends, injected time and
isolated native-WSL HOME/XDG/repos; do not call live providers.

| Contract | Independent observable acceptance |
| --- | --- |
| I-01 | Actual fake initial failure -> real CLI force -> one successor. Reject correction, sequence, wrong block, active/abort/hold, missing evidence and conflicting owner. Concurrent force calls with deterministic barriers create one relation/effect; duplicate original-source call after completion returns it unchanged. |
| I-02 | Assert actual retry argv bytes equal independently constructed base + exact suffix; original hashes unchanged. Another failed initial successor recovers with one note and exact same chat, no chain fabrication. Record-byte tampering, wrong digest/owner and missing parent evidence reject both creation and replay. Schema/model required/null/coercion cases agree. |
| I-03 | Real publication/tick/dispatch paths never launch before ready. Crash before/after artifact and final-ledger boundaries; restart retains ownership then eventually publishes one effect or completes cancellation. Force/abort/tick races block late launch. No test supplies production reservation/adoption/dispatch guards. |
| I-04 | Fake Cursor success -> normal staging -> authenticated fresh B review -> completion. No new chat, no review before Cursor success, completed count zero during recovery and advances only after review. Staged/unstaged/untracked sentinel content and index unchanged immediately after force; standalone output remains staged after normal success. |

Tests must expose the original missing-effect defect: successful CLI publication
without a retry effect must fail the end-to-end acceptance. Test failures after
successor creation as well as before it. Preserve Phase 23.1 read-only guarantees,
legacy no-flag timeout prompt identity and existing review/abort behavior.

## Validation

```bash
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23_2*.py tests/integration/test_phase23_2*.py
.venv/bin/python -m pytest tests/unit/scheduler/test_phase23_1*.py tests/integration/test_phase23_1*.py tests/integration/test_cursor_timeout_retry.py tests/integration/test_phase17_6_abort_lifecycle.py tests/integration/test_phase20_8_sequence_review_retry.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Use isolated Python 3.11+ `.venv`/`uv`; verify local CLI help. Report I-01–I-04
implementation, production callers, tests and exact executed/failed/unexecuted
checks. Missing dispatch, crash convergence or required tests is incomplete
delivery. Do not install the feature or run a live recovery for validation.

## Risks Or Recovery Notes

This run owns the entire initial standalone publication lifecycle, including
reconciliation and abort. Those requirements cannot be deferred to 23.4.
An already created B or sequence relation is a real scope boundary, not a null
placeholder. Preserve the stopped combined patch as reference; selectively adapt
only code demonstrated by this run's contracts. Planning does not launch a run.

## OpenQuestions

None. Phase 23.1 acceptance is a prerequisite, not an assumed implemented feature.
