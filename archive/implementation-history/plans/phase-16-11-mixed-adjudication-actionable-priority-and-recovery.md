# Phase 16.11 — Mixed adjudication actionable priority and recovery

## Goal

Make PR-review v2 remediate actionable threads in a mixed adjudication batch, while preserving non-actionable replies as deferred durable work. Add explicit, idempotent recovery for run prv2-16e2cd4c29eaec847405da3ae83d6174, which is waiting_for_user with one reply, two actionable decisions, and no fix prompt.

## Non-Goals

Do not weaken public-write confirmation, auto-post or resolve threads, rewrite historical SQLite rows/effects, fabricate a fix prompt, implement crypto-sentinel fixes here, change generic recovery/carriers, limits, ai_dev_loop.yaml, or skills.

## Scope

External-adjudication schema and runner, domain contracts/reducer/states, control recovery, status decision summary, tests, and operational docs.

## Out of Scope

Direct SQLite repair, mutable GitHub-data adoption, public writes during recovery, and unrelated Phase 16 work.

## Required Context

The current contract permits fix_prompt only when every decision is actionable. The reducer consequently sends every mixed result to WaitingForUserState and emits only post_thread_reply. The current cycle-3 frozen evidence demonstrates this defect.

## Cursor Rules And Skills

No repo-local AGENTS.md, .cursor/rules, or .cursor/skills were found. Recheck before editing. Follow the Phase 16.3, 16.4, 16.6, 16.7, 16.8, and 16.9 plans.

## Architecture Guardrails

- Reducer stays pure; no filesystem, SQLite, GitHub, or Codex access.
- ProtectedResultStore owns bytes and hash checks; state holds ArtifactRefs only.
- Any actionable decision requires a protected fix prompt; zero actionable decisions forbid one. Actionable records never have replies; non-actionable/uncertain records always do.
- Actionable IDs are ordered, unique frozen-set subsets and retain run/cycle/PR/head binding.
- Deferred replies are immutable ReplyIntents and cannot be posted before remediation.
- Recovery appends a concurrency-checked event and new deterministic effect; it never rewrites old rows/events/effects or creates public write effects.
- Fail closed on stale head/snapshot, malformed/missing artifacts, duplicate recovery, or claimed write.
- Preserve all-actionable and reply-only behavior.

## Implementation Plan

1. Update the external-adjudication JSON schema, Pydantic payload, conversion, and Codex wrapper prompt: fix_prompt_text/ref is required if any decision is actionable and forbidden if none is. Preserve strict per-thread reply validation.

2. Add domain helpers for actionable IDs and deferred replies. Extend affected states with backward-compatible defaulted deferred-reply fields. Route any evidence with actionable IDs to RunningLocalFixState/RunLocalFixEffect; route reply-only batches to existing WaitingForUserState.

3. Carry deferred replies through RunningLocalFixState and PublishingFixState. After accepted fix, publication, and resolution of actionable IDs only, enter WaitingForUserState if replies remain; otherwise keep the existing next-cycle route. Pause/fail/abort must preserve deferred replies and not emit a reply.

4. Add explicit legacy recovery, e.g. RecoverMixedAdjudicationRequested plus a pr-review resume option. Allow only WaitingForUserState with actionable and reply decisions, no fix_prompt_ref, exact frozen/binding/trigger integrity, and matching unclaimed queue-head reply. Persist operator recovery evidence, transition to AdjudicatingState on the original frozen snapshot, and issue a fresh deterministic adjudication effect keyed by recovery operation plus legacy result-ref digest. Re-adjudicate to create a real protected mixed fix prompt. Reject modern mixed, all-actionable, reply-only, stale, malformed, and duplicate cases.

5. Add bounded status decision output: decision counts, actionable/deferred counts, and next safe action. Do not expose raw prompts, patches, sessions, auth, or JSONL.

6. Document ordering, recovery command/preconditions/no-write guarantee, and decision inspection.

## Testing Criteria

Automated tests are required.

- Unit model/reducer: valid mixed enters running_local_fix with exact actionable subset and deferred replies; missing mixed prompt and invalid reply shapes fail; all-actionable/reply-only behavior remains unchanged; IDs/bindings/attempts/refs deterministic.
- Reducer lifecycle: no reply before local fix; accepted fix publishes/resolves actionable IDs only then waits for deferred reply; paused/failed/aborted local fix retains replies; confirmed final reply resumes observation.
- Control/recovery: legacy current-state shape yields exactly one new re-adjudication effect and no GitHub write; repeat-safe; malformed/stale states unchanged; ordinary confirmation does not invoke recovery.
- Integration: fake Codex mixed output includes prompt; local carrier sees actionable IDs only; fake GitHub sees no early reply; write retries/reconciliation remain idempotent.
- Target files: tests/unit/pr_review_v2/test_models.py, test_reducer_paths.py, test_correction_findings.py, test_phase16_7_correction_regressions.py, tests/integration/test_phase16_8_control_matrix.py, test_phase16_8_existing_pr_happy_path.py, plus focused recovery tests.
- Use TMPDIR=/tmp for WSL pytest when needed; run focused tests, relevant Phase 16.8/16.9 integration tests, Ruff, formatting, and git diff --check.

## Validation

Simulate in memory the exact current run shape: waiting_for_user, cycle 3, three frozen IDs, one reply, two actionable IDs, no fix prompt. Explicit recovery emits only fresh adjudicate_threads work for the same snapshot/head. The upgraded mixed result emits RunLocalFixEffect for exactly the actionable IDs. Accepted local fix then publication/resolution reaches waiting_for_user for deferred reply. No historical effect/event is rewritten and repeated recovery is idempotent.

## Risks Or Recovery Notes

Do not execute recovery while implementing. After staged review/install, controller invokes explicit recovery, inspects status, and launches only with approval. If validation fails, leave the run safely waiting_for_user.

## OpenQuestions

None.
