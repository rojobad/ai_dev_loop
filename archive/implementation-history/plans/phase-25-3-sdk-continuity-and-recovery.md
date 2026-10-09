# Phase 25.3 — Exact SDK continuity and safe recovery

## Goals

Replace 25.2's SDK recovery guards with supported timeout/capacity continuation,
manual initial/correction recovery and sequence-leaf recovery. Preserve exact
conversation/store/B identity and make owned crash/abort reconciliation converge
without duplicate prompts or releasing uncertain repository ownership.

## Non-Goals

New retry policy, exactly-once provider guarantees, automatic replacement agents,
native-store repairs, SDK force expiration, activity timeouts or parallel agents.

## Scope

Native-run reconciliation, process-tree termination proof, error classification,
automatic timeout/capacity and explicit cursor-retry contracts, Phase 23 successor
lineage, correction/review budgets, sequence replacement, cancellation races,
affected schemas/readers/docs and production-path tests.

## Out of Scope

Root YAML, Phase 24, billing/metrics summaries, old CLI-session migration,
destructive Git, direct native-store edits, workstation service changes, live
agent activity, package installation and planning/review skill changes.

## Required Context

Read [the overview](phase-25-cursor-sdk-and-token-usage.md), accepted
[25.2](phase-25-2-sdk-identity-worker-and-evidence.md) contracts/findings and
[POC crash/cancel evidence](../findings/phase-25-sdk-poc/README.md). Verify W-01–W-04
and temporary guard locations. Inspect current application Cursor timeout/retry,
recovery check, initial/correction/sequence recovery services and evidence,
restart reconciler, abort services, attempt_service/systemd_backend; domain
recovery records, state/events/lineage and actual public CLI callers. Native
runtime death in the POC left a tool alive and a stale `running` run. Registered
get-run -> cancel -> confirm -> exact-agent resume succeeded after termination;
detached cancellation failed. These facts define mandatory regression coverage.

## Cursor Rules And Skills

Follow `AGENTS.md` and all eight overview `.cursor/rules/`. Read
`.agents/skills/create-cursor-plan/SKILL.md`; preserve it and
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. No `.cursor/skills/`
exists. Update directly affected loop/abort/state rules to the implemented native
identity and recovery path, keeping reviewer B and Git authority intact.

## Architecture Guardrails

- Retain the original conversation key/store owner and exact returned agent ID
  across successor runs. Native run IDs change only for durably authorized new
  prompts. Preserve the immutable blocked source, evidence ancestry, prompt
  hashes, reviewer B and review budget; sequence recovery keeps ordinal/relation.
- Authenticate old attempt ownership and prove whole-tree termination before
  resetting native active state, redispatch or reservation release. An API
  `cancelled` state or closed stream alone cannot prove dead tools. Missing bus/
  unknown process evidence retains the existing hold and safe next action.
- Reconciliation operations are worker effects fenced in the ledger. Use a
  fresh client with the same authenticated workspace/store, exact agent/run,
  explicit runtime and credential. No tick-process SDK singleton or unowned
  background service. Native reads/cancel do not authorize a fresh prompt.
- Use get_run with explicit local/agent binding to register the handle, cancel a
  proven stale nonterminal run, then confirm terminal status before exact resume.
  For terminal runs, read/replay without cancel; do not trust supports flags over
  actual state/operation contracts. No `SendOptions.local.force` fallback.
- Keep current fixed-timeout automatic retry count/delay, capacity policy and
  manual `scheduler cursor-retry`, `--check`, `--force`, review retry/extend
  semantics. `cursor-retry --force` is scheduler authority, not an SDK flag.
- Do not turn a generic `InternalServerError` or a transient network timeout
  into usage-limit evidence. Honor safe structured codes, retry_after, native
  status and the authenticated attempt that actually failed.

## Implementation Plan

**R-01 — Confirmed failure to same-agent continuation.** Wire initial and
correction timeout/capacity flows to reconcile the exact native run first.
When process absence is proved and a stale native run remains running, perform
the public get-run/cancel/confirm sequence and persist its evidence. Existing
durable policy then authorizes one new native run for the exact frozen prompt or
correction envelope. Preserve automatic counters across replays. Busy indicates
unfinished work to reconcile, not permission to expire it or create a new agent.

**R-02 — Manual successors and sequence lineage.** Extend actual `--check` and
`--force` evidence paths for SDK runtimes, including initial/correction/eligible
sequence leaf failures and repeated successors. Inherit stable conversation
owner/store/options and exact B, prompt/fix evidence, ceiling/counters. Retain
source immutability and the existing publish/adopt/cancellation transaction and
sequence relation. Reject a stale sequence leaf lacking its current relation.
SDK get-run operations remain observations/cancellations until the scheduler
durably publishes authorization for the successor's prompt.

**R-03 — Ambiguous create/send and completed-result loss.** Record a native
run-inventory baseline for the exact agent before sending, alongside dispatch
intent and attempt fence. After a lost send response, compare that baseline with
public native run listings for the same authenticated agent/store. Adopt a unique
new run only when its ownership and dispatch can be proved from the single
authorized sender, recorded inventory and native evidence. Do not match merely
by timestamp, model text or latest run. Missing/conflicting candidates remain
blocked with ownership held and no automatic resend. If the SDK cannot supply
the promised binding evidence, document that unsupported ambiguity and retain
the safe blocked branch; do not invent inference exactly-once guarantees.

If create was accepted before agent ID publication and exact identity cannot be
proved, retain creation uncertainty; do not create a second agent. Expose
inspection/abort as the safe next action after proving process absence. A fresh
submission requires explicit authorization and does not mutate the source.

If a known native run completed but the worker lost its terminal envelope,
retrieve/replay the exact result, republish missing scheduler evidence and ingest
once without another prompt. Prove convergence for all cases with sufficient
identity: known running run -> proved stop/cancel -> authorized continuation,
known terminal run -> recovered outcome -> original workflow checkpoint.

**R-04 — Abort/restart/process ownership.** Test and complete SIGKILL of worker,
launcher and actual runtime, orphan tools, unavailable backend observation,
late result, result publication crash and cancellation during native recovery or
sequence adoption. Persist abort before stopping; stop only owned units/groups;
never stage/review/send after cancellation. Once exact processes are absent and
native cancellation/result reconciliation is confirmed, complete the durable
abort/failure transition and release only reservations permitted by that state.
Remove 25.2 guards only where all now-supported paths have contract coverage.

Boundary: durable classified failed/uncertain attempt + held reservation ->
proved owned process termination -> exact native read/cancel/result evidence ->
ledger reconciliation -> existing explicit/automatic retry decision -> at most
one new native prompt. Interruptions preserve pending intent and ownership;
replays converge through fences and terminal evidence. Abort wins over retry/
handoff and never rolls back user edits or rewrites source artifacts.

## Testing Criteria

Add `tests/unit/scheduler/test_phase25_3_sdk_recovery.py`,
`tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py` and
`tests/integration/test_phase25_3_sdk_recovery.py`. Use public-API SDK fakes,
owned synthetic descendant process trees, fake systemd control observations,
deterministic barriers and temporary native-WSL repos/stores. Fake Codex supplies
authentic schema-conforming findings and completed reviews.

| Contract | Independent production-path acceptance |
| --- | --- |
| R-01 | Timeout/capacity tick flows prove dead tools, get exact stale run, cancel/confirm and continue same agent once; limits/delays and unknown observation holds remain unchanged. |
| R-02 | Actual cursor-retry check/force, initial/correction successor and sequence replacement keep source bytes, conversation/store/B/budgets; stale leaf is rejected and accepted leaf advances normally. |
| R-03 | Kill between native send and ID publication: unique authenticated run is adopted, multiple/missing runs block with zero resend. Lost terminal envelope is recovered without inference; lost create identity never creates a replacement. |
| R-04 | Launcher-only death and runtime death with live tools retain ownership; eventual proved cleanup converges. Abort races reject late results/prompts/checkpoints while preserving worktree/artifacts. |

Tests must run real scheduler service/CLI callers and production locks/fences.
Do not repair the store, populate missing ownership or introduce synchronization
only in the fake to conceal a production race. Error regression fixtures include
the pinned SDK's generic server errors and contradictory capability flags.

## Validation

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp .venv/bin/python -m pytest -q tests/unit/scheduler/test_phase25_3_sdk_recovery.py tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py tests/integration/test_phase25_3_sdk_recovery.py tests/integration/test_cursor_timeout_retry.py tests/integration/test_phase23_1_cursor_recovery_check.py tests/integration/test_phase23_2_initial_recovery.py tests/integration/test_phase23_3_correction_recovery.py tests/integration/test_phase23_4_sequence_recovery.py tests/integration/test_phase17_6_abort_lifecycle.py tests/integration/test_phase17_6_restart_reconcile.py
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src/ai_dev_loop
.venv/bin/python -m mkdocs build --strict
git diff --check
```

Report R-01–R-04, actual authoritative failure evidence, recovery convergence,
known unsupported ambiguous-create cases and exact results in
`archive/implementation-history/findings/phase-25-3-sdk-recovery-findings.md`.
Keep full-suite gate assigned to 25.5; don't relax mandatory safety coverage.

## Risks Or Recovery Notes

An SDK exception is not proof that no work began. Permanent ambiguity has the
explicit safe blocked/abort branch; it is not permission for a hidden repeat.
Treat same-agent cancellation only as native-state reconciliation after proved
termination. Do not use private runtime PIDs, force expiration or store edits as
production recovery. Missing store/agent is actionable failure, not replacement.

## OpenQuestions

None. If native evidence cannot establish ownership for unique-run adoption,
retain the specified blocked branch and report the limitation; expanding recovery
authority beyond that contract requires a new operator decision.
