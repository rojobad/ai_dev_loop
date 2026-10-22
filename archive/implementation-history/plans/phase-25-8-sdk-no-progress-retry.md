# Phase 25.8 — SDK no-progress diagnosis and one safe automatic continuation

Prepared 2026-10-21 against main `7ddda1a60cadceee35f0af8547a896f1b2ad0bdb`.
One standalone implementation run. The operator requested plan creation and run
preparation only; starting that run remains a separate operator action.

## Goals

Make a known terminal Cursor SDK no-progress failure understandable and allow
one safe automatic continuation of the pending logical implementation turn,
without requiring a supervisor to issue `implementer-retry --force`.
Reuse native reconciliation and the existing same-agent recovery lineage.

## Non-Goals

Prevent every SDK connection failure, guarantee progress or exactly-once model
side effects, replace the SDK's internal retry loop, or turn all terminal errors
into automatic retries. One automatic continuation can repeat work after the
last provider checkpoint; the fixed recovery note must preserve partial changes.

## Scope

Authenticated terminal failure classification, truthful diagnostics, a durable
one-use automatic authorization with a 60-second delay, production tick and
recovery wiring, standalone and current sequence-leaf continuity, abort/manual
retry/retention interaction, historical compatibility, focused regressions and
operator documentation. All contracts C-01 through C-06 are one delivery.

## Out of Scope

Dashboard UI/widgets/UX or token analytics; Phase 29 formatting/acceptance;
SDK/dependency upgrades or installed vendor patches; storage backend changes;
SDK retry/heartbeat overrides; new generic recovery framework; provider/model
switching; changing existing timeout/capacity/reviewer retry budgets; new timers;
live model calls in tests; live ledger/native-store cleanup; installation or
workstation service operations; implementation-agent Git commits/push/PR/merge;
`ai_dev_loop.yaml` and skill edits. Do not change frozen submitted inputs.

## Required Context

Read `AGENTS.md`, `docs/referencia/{cli,configuracion}.md`, and
`docs/operacion/{estado-artefactos,observabilidad,seguridad-privacidad,
desinstalacion-limpieza}.md`. Read accepted Phase 25.6 and 25.7 A/B/C findings.
Current code, schemas, CLI help and tests govern retired/historical instructions.

Inspect:

- `integrations/cursor_sdk/{adapter,port}.py`, the public pinned Python SDK
  status-event/RunResult shapes and the packaged native initializer;
- `scheduler/cursor_attempt_runner.py`, especially `_run_sdk_turn_operation`
  terminal stream capture and `_run_sdk_reconciliation_operation`;
- application `cursor_workflow_service`, `cursor_sdk_reconciliation`,
  `cursor_sdk_journal`, `cursor_sdk_progress`, `cursor_sdk_successor`,
  `cursor_initial_recovery`, `cursor_timeout_retry`, `sequence_cursor_recovery`,
  `tick`, `safe_actions`, `abort`, and `cursor_sdk_retention`;
- domain state/events, SDK recovery types, recovery publication/outbox and
  sequence lineage stores, SQLite migrations and published schemas;
- SDK recovery/dispatch-uncertainty, sequence recovery and retention tests.

Verified prerequisites and incident evidence:

1. A/B/C are accepted and merged; schema is 19. New SDK bindings freeze native
   SQLite; the owner initializer and remaining-operation deadline already work.
2. The pinned Python and bundled TypeScript SDK are both 1.0.37. The Python
   public `RunResult`/`RunSnapshot` does not expose `RunError.code`. A terminal
   status message is available through the public SDK event stream.
3. The Phase 29 correction returned its handle in 2.857 seconds, then failed
   after about 9m45s with `timed_out=false`. The public status event was
   `type=status`, `status=ERROR`, with the exact message:
   `Agent turn stopped after repeated resume attempts made no progress`.
   The metadata, captured events and dispatch journal hashes were verified.
   These are internal SDK resume attempts, not repeated app resume_agent calls.
4. Reconciliation found exactly one attributable native run, already `error`,
   performed no cancellation and returned `continuation_ready`. The manual
   successor kept owner/key/agent/backend/layout, correction iteration and the
   existing recovery mechanism. Its effective prompt includes the fixed recovery
   note, so its digest differs from the original correction envelope by design.
5. Current generic failure handling deliberately blocks after reconciliation;
   only existing capacity/timeout paths have their respective automatic policy.
   The generic `continuation_ready` branch misleadingly says that no safe
   continuation was established (`cursor_sdk_reconciliation.py`).
6. Existing SDK force recovery publishes one authenticated same-agent successor,
   preserves pending initial/correction work, and supports current sequence-leaf
   adoption. Reuse these services and their fences, not a shell invocation of
   `implementer-retry --force` from a tick.

Primary supporting explanation (not runtime authority):
https://forum.cursor.com/t/cursor-sdk-long-opus-5-5-local-runs-end-mid-turn-after-file-reads-canceled-this-operation-was-aborted-repeated-resume-attempts-made-no-progress/173939
The bundled SDK stops after two resumed attempts stream without a checkpoint and
marks the display error retryable. The message alone does not prove the network,
provider or storage was the underlying cause. Support's older upgrade advice is
not a reason to change the current 1.0.37 pin.

The private incident report/production artifacts are supplementary evidence;
implementation tests must use synthetic fixtures, not copy production prompts,
native IDs, host paths or transcripts, and must not require live-state access.

## Cursor Rules And Skills

Follow AGENTS.md and:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Respect `.agents/skills/create-cursor-plan/SKILL.md` and
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md` without editing them
or switching your implementation role. There is no `.cursor/skills/` directory.
Do not invoke handoff/controller/recovery workflows or create nested agents from
inside the implementation attempt. Correct the narrow manual-only wording in
AGENTS.md/the loop rule if needed to document this approved exception; preserve
all other generic-failure and operator Git/installation boundaries.

## Architecture Guardrails

The ledger, authenticated attempt evidence and protected artifacts are authority.
Keep public SDK normalization in the integration/worker layer, authorization in
the application/domain layer and durable operations in the existing stores and
outbox. Never infer retry eligibility from a human summary, arbitrary transcript
substring, lack of output, CPU, or a native `running` label.

Preserve repository reservation, exact owner/key/agent/backend/layout, reviewer B,
frozen models/limits, raw pending correction, reviews completed, effective review
ceiling and existing partial work. A retry is a continuation of the same logical
turn; it is not an accepted implementation or another review. No new agent,
re-admission requiring a clean baseline, speculative resend, broadened native
cancellation or autonomous Git operation is allowed.

The approved new policy is one automatic continuation per logical turn, due no
earlier than 60 elapsed seconds after durable safe authorization. A new attempt,
run ID, recovery note or successor does not create another allowance. Only a
genuinely new initial task or a new authenticated reviewer correction creates a
new logical turn. Existing timeout/capacity policies remain independent and their
counters must also continue to be carried as before.

## Implementation Plan

### C-01 — narrowly classified, authenticated terminal failure evidence

Recognize application category `cursor_sdk_resume_no_progress` only from the
known public SDK terminal status event with the exact complete message above
(outer whitespace trimming only), matching the owned agent/native run and a
consistent final native status `error`. This category is an application label,
not an invented SDK error code. Prefer a real structured field only if the
pinned public surface demonstrably provides it; no private SDK inspection at
runtime or dependency upgrade.

The production stream/worker must persist a bounded typed/versioned failure
witness linked to the attempt, offset/capture digest, journal and exact native
identity before eligibility is trusted. The diagnostic can share existing
evidence where appropriate. A word match in assistant/thinking/tool text, an
arbitrary exception or nested payload is not a status event. Do not classify
partial/contradictory/corrupt or unverifiable truncated evidence as eligible.
Missing diagnostic evidence means unknown/generic failure and the current manual
path. Safe summaries expose the category and action, never raw messages/secrets,
prompts or full native/session IDs. Do not manufacture a network root cause.

### C-02 — truthful diagnosis and next action

Distinguish terminal native failure from timeout/uncertain state and distinguish
reconciled continuation readiness from authorization to retry. Fix the generic
summary for `continuation_ready`: it must not assert reconciliation failed.
For manual-only or exhausted cases say that a safe continuation is established
but requires the supported explicit retry. For pending automatic cases expose
the one-use allowance, scheduled UTC due time and a scheduler-tick/wait next
action; do not ask a supervisor to force another successor during that wait.

Update supported CLI status/history/timeline projections and docs as necessary,
with safe optional/version-compatible fields. Preserve honest historical
unknown values and read-only inspection. No Dashboard presentation work.

### C-03 — one durable automatic authorization after safe reconciliation

The only new automatic case is C-01 plus all existing continuation predicates:
owned process tree proved terminated; authenticated complete failed-turn and
identity evidence; same exact agent and one attributable native run; fresh native
observation confirms `error`; reconciliation is authenticated and
`continuation_ready`; no abort, disposal or conflicting reservation; initial turn
or current sequence correction remains eligible. No cancellation is needed for
an already terminal error. Existing reconciliation may still resolve other
states under its old policy, but active/cancelled/unknown/ambiguous runs do not
qualify for this new automatic authorization.

Persist authorization, budget consumption and UTC `due_at = authorized_at + 60s`
before publication/dispatch. Maintain one authenticated logical-turn key across
source/successor/manual recovery wrappers, using the original base turn and
pending-review binding rather than the changing effective recovery-prompt hash.
Represent consumed count strictly as 0 or 1, excluding booleans/coercion. Reserve
the allowance transactionally at authorization; replay reuses it. A reset is
allowed only for a real new initial/correction boundary.

Retain repository ownership and continuation references during waiting and
publication. No idle gap may admit another run or permit retention disposal. Use
the existing native recovery/successor machinery and outbox to publish one
continuation on or after the due time. A typed waiting record/state may be
needed; choose the smallest structure compatible with existing reducers and
fences. Do not introduce a second generic recovery engine. If existing blocked
sources remain immutable, coordinate the pending authorization/reservation and
sequence projections so the scheduler cannot abandon an automatically recoverable
leaf merely because the source is blocked.

Revalidate owned authority, current leaf, abort/disposal and native safety before
send. Changed/conflicting/missing evidence holds without a prompt. Native
`finished` with valid recoverable output follows existing replay rather than
sending another prompt. A second no-progress failure is reconciled and held for
manual action; it never schedules another automatic retry of that logical turn.

### C-04 — continuity and crash/race convergence through actual callers

Apply to standalone initial and correction turns and a current sequence leaf.
Reuse same-agent successor publication and authenticated sequence adoption when
required by the existing architecture. Preserve the same ordinal and frozen
entry, reviewer and reviewed patch/fix evidence, review counts/ceiling and
checkpoint/final-stage behavior. A failed automatic continuation cannot create
another review or advance a phase. Recovery prompts must use the existing fixed
note and exact base instructions, without unbounded note nesting or rewritten
reviewer findings.

Ticks and restarts converge from interruption before authorization, after budget
reservation, between artifact/ledger publication, before/after sequence adoption
and before dispatch/result ingestion. Multiple ticks, duplicate ingestion and
concurrent manual `implementer-retry --force` must share one matching recovery
publication, reservation and consumed budget. While pending, manual retry returns
or reports that same pending continuation, without duplicate dispatch or an
additional automatic allowance. After exhaustion, existing explicit manual
recovery remains possible and carries consumed count 1 for the same turn.

Abort wins before dispatch/adoption, fences pending work and releases only after
existing native/owned-attempt obligations are resolved. A stale sequence leaf is
not revived. Retention preview/manual/maintenance all treat pending/due/publishing
retries and their lineage as recoverable and ineligible; definitive completion of
the resulting lineage still starts the normal 168-hour clock. No live cleanup.

### C-05 — persistence, historical and adjacent-policy compatibility

Add typed models, schemas, serializers, path dispatch, migrations and readers
together for changed persisted contracts. Schema 19 is the inspected baseline;
allocate a new migration only when required. Keep frozen submitted contexts,
plans/prompts and historical canonical hashes byte-identical. Do not silently
extend old canonical v1/v2 recovery objects with fields that change their hashes.
New diagnostic/automatic-authority forms must have explicit versioned writers
and validated readers; absence/null are explicitly not eligibility. Historical
records without the new witness/budget authority stay manual and are not
retroactively retried by scanning old transcripts.

Preserve Phase 25.6 creation-readiness/no-duplicate-send and Phase 25.7 native
storage/deadline/retention behavior. Other SDK errors (including generic network,
server, authentication, configuration or missing-store failures), Codex behavior,
capacity/timeout waits and reviewer-retry budgets do not acquire this automatic
policy. Do not refresh the no-progress allowance through those recovery paths.

### C-06 — executable evidence and operator handoff

Implement the regressions below through production entry points. Update CLI,
configuration/observability/artifact/recovery docs to state the enabled narrow
policy, 60-second earliest dispatch, one-use logical-turn limit, manual fallback,
safe diagnostics and historical behavior. Apply focused AGENTS.md/loop-rule
alignment only for this exception. Report contract IDs, production callers,
red/green evidence and remaining gates in
`archive/implementation-history/findings/phase-25-8-sdk-no-progress-retry.md`.

## Testing Criteria

Use temporary HOME/XDG/repositories/ledgers, fake SDK/CLIs/process backends and a
controlled UTC clock. No credentials, live artifacts, real models or workstation
timer operations. Existing production-path fake SDK fixtures may be extended
with synthetic terminal status events. Do not inject the missing classifier,
authorization, reservation or sequence adoption directly from the positive test.

| Contract | Mandatory production behavior and level |
| --- | --- |
| C-01 | Adapter/worker stream emits canonical status ERROR, final native error and exit 1 with timed_out=false; normalized witness authenticates. Same phrase in assistant/tool text, unknown/prefixed messages, mismatched identities, missing/truncated/corrupt/contradictory evidence never authorizes auto retry. Unit and owned-worker integration. |
| C-02 | Generic continuation_ready reports reconciliation success/manual retry, eligible pending reports wait/due/count, exhausted reports manual action, ambiguous stays unknown/held. CLI status/history/timeline contain no raw prompt, secret, full native IDs or claimed network cause. Old records remain read-only and readable. |
| C-03 | Error -> worker stopped -> exact reconciliation -> durable authorization -> no send one microsecond before due -> one continuation on/after 60 seconds -> completion. No cancellation for terminal error; no review before success. Run this through submission/ticks and the real SDK worker fake, not an isolated retry helper. |
| C-03/C-04 | Failure again after the automatic attempt yields zero second automatic attempts after any number of ticks/restarts. Explicit manual successor and timeout/capacity lineage retain consumed count; a genuinely new authenticated correction gets one fresh allowance. Recovery wrapper digest changes do not reset the budget. |
| C-04 | Standalone initial/correction and current sequence leaf keep exact identities, reviewer, base prompt/fixed note, reviews/effective extended ceiling, partial work and ordinal. Accepted non-final retry checkpoints once and materializes the next phase once; final retry leaves staged changes without a commit. |
| C-04 | Deterministic synchronization for two ticks/claims, manual force vs automatic publication, and abort vs pending/adoption. Fault injection at intent/artifact/relation/adoption/dispatch boundaries resumes the same durable authorization and sends at most one authorized continuation. A stale claimant/source cannot overwrite the winner or revive an aborted leaf. |
| C-04/C-05 | Running native candidate, zero/multiple candidates, lost process, corrupt identity/witness/journal, missing store, abort/disposal and foreign reservation refuse automatic dispatch. A finished candidate uses existing result replay. Pending retry/lineage prevents disposal even at age >168 hours; terminal lineage later regains ordinary eligibility. |
| C-05 | Typed model/JSON-schema agreement and genuine baseline canonical fixtures; new writers do not rewrite old contexts/recovery records. Historical missing witness remains manual. Existing timeout/capacity, busy-first-send, generic failure/manual retry and Codex-only tests stay intact. |

Expected additions: `tests/unit/scheduler/test_phase25_8_sdk_no_progress.py` and
`tests/integration/test_phase25_8_sdk_no_progress.py`, with representative adapter,
SDK recovery, sequence recovery, lifecycle and CLI regressions in their current
files. Keep deterministic race tests bounded; do not use sleep as synchronization.

## Validation

Mandatory initial focused selection (add actual failed/affected node IDs where
the implementation touches more callers):

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run --extra cursor-sdk python -m pytest -q --tb=short \
  tests/unit/scheduler/test_phase25_8_sdk_no_progress.py \
  tests/integration/test_phase25_8_sdk_no_progress.py \
  tests/unit/test_phase25_1_sdk_adapter.py \
  tests/unit/scheduler/test_phase25_2_sdk_worker.py \
  tests/unit/scheduler/test_phase25_3_sdk_recovery.py \
  tests/unit/scheduler/test_phase25_3_sdk_dispatch_uncertainty.py \
  tests/unit/scheduler/test_phase25_6_sdk_busy.py \
  tests/unit/scheduler/test_phase25_7_sdk_deadlines.py \
  tests/unit/scheduler/test_phase25_7_sdk_lifecycle.py \
  tests/unit/scheduler/test_phase23_4_sequence_recovery.py \
  tests/integration/test_phase25_3_sdk_recovery.py \
  tests/integration/test_phase27_3_implementer_sequences.py
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m mypy src
uv run mkdocs build --strict
```

Run schema/migration and inspection tests affected by new persisted forms, and
re-run the failed/affected selection after corrections. Report exact commands,
outcomes and duration; passing counts do not replace mandatory contract evidence.
Repository-wide pytest, build/wheel/isolated install and live model acceptance
belong to the separate post-acceptance pipeline/operator gate, not every review.
Do not operate real integration installation, scheduler services or live stores
from this implementation run.

## Risks Or Recovery Notes

The SDK's stop is a cost/progress safeguard. This policy grants only one delayed
continuation after proof; it does not disable that guard or guarantee a provider
checkpoint exists. Keep manual intervention after another failure. Classification
from untrusted/free-form text or resetting the counter on successor creation
would create an unbounded retry loop and fails the contract.

Do not turn a pending sequence retry into terminal sequence failure that cannot
be adopted, release a worktree during publication, or let maintenance erase a
recoverable owner's store. Use existing fenced transitions and durable relations.
Do not rewrite old incident records to test the new behavior. Missing required
public SDK evidence or a genuine contract conflict blocks dependent work with
specific evidence, rather than broadening message heuristics.

## OpenQuestions

None. The operator approved improved diagnosis and one safe automatic retry for
this known error, with a prepared but unstarted standalone implementation run.
This plan specifies one allowance per authenticated logical turn and a fixed
60-second backoff; preparing the plan does not activate that product policy or
authorize a live retry. New unresolved safety decisions must be reported before
dependent implementation.
