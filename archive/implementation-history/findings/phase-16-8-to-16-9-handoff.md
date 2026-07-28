# Phase 16.8 -> Phase 16.9 Handoff

Date: 2026-07-28

## Executive status

Phase 16.8 reached controlled live acceptance for the `pr-review-v2`
**existing-PR happy path**. The final controlled run reached durable
`completed` after two external review cycles and one local correction.

This is an intentionally narrowed acceptance decision. The original Phase
16.8 resilience matrix was not completed in full; the remaining work is
recorded in [`PHASE_16_8_DEFERRED_ISSUES.md`](../../../PHASE_16_8_DEFERRED_ISSUES.md).
Those items are not regressions to be silently fixed during Phase 16.9.

The user explicitly accepted that trade-off in order to proceed with the v2
cutover. Phase 16.9 must preserve the current behavior and may record new
evidence, but it must **not** implement, broaden, or close any deferred issue
unless the user separately authorizes a future resilience phase. A deferred
item should be scheduled only if it occurs sufficiently often or the user
explicitly reprioritizes it.

## Gate A — automated and staged evidence

The final Gate A implementation includes:

- optional, disabled-by-default `accept_bot_thumbs_up` no-findings evidence;
- strict exact-trigger, allowlisted-actor, post-trigger-timestamp and
  no-eligible-thread semantics for the `+1` completion rule;
- protected observation/reaction evidence and owner-only artifact readers;
- durable SQLite effect/claim/lease/timer control, exact Codex session
  continuity, deterministic local carrier reuse, and write reconciliation;
- temporary `ai_dev_loop pr-review-v2` namespace, with legacy `pr-review`
  intentionally retained until Phase 16.9;
- a production-assembled simulated existing-PR workflow using local fake
  GitHub/Cursor/Codex processes, temporary Git/bare-remote state, a real
  control/supervisor boundary, and durable completion.

The final staged-review correction specifically exercised the local Codex
process boundary and the controlled supervisor-launch/ownership boundary. It
reported the following validation:

| Validation | Result |
|---|---:|
| Existing-PR production-assembled happy path | 1 passed |
| Local/Codex/supervisor/control regressions | 59 passed |
| Phase 16.1 A/B barrier | 12 passed |
| Full suite | 1,870 passed |

The staged reviewer found no Gate B-blocking findings after those corrections.
The full original crash/recovery matrix remains deferred as documented below;
the table above is not evidence that all original Phase 16.8 criteria were
completed.

## Gate B — controlled live acceptance

### Target and safety boundary

- Target: isolated clean checkout of `rojobad/parish360-poc`.
- Controlled PR: #8, left open after acceptance.
- Existing PR #1 and the user's original dirty checkout were not used as the
  write target.
- No merge, force-push, rebase, branch deletion, PR closure, or destructive
  cleanup was performed.
- Exact Codex/Cursor identifiers, prompts, patches, reaction bodies and tokens
  are intentionally not reproduced in this handoff.

### Final run

| Field | Safe result |
|---|---|
| Run | `prv2-e8dc2514a194af3b63483306e7d07219` |
| Origin | existing PR |
| Final state | `completed` |
| Run version / journal entries | 17 / 16 |
| External cycles | 2 of 2 |
| Local iterations | 1 of 1 |
| Final head prefix | `a2960bdaa299` |
| Last durable transition | 2026-07-28 11:33:57 UTC |
| Supervisor / lease at completion | not live / inactive |
| Retries or error effects | none; all effects succeeded on attempt 1 |

### Live lifecycle observed

1. `prepare` created a durable existing-PR run without external writes.
2. Explicit `start` opened the write gate and posted the first idempotent
   `@codex review` trigger.
3. The worker polled at approximately 60-second intervals. It observed an
   actionable review, then performed exact-session external adjudication.
4. One local carrier created or reused the exact Cursor chat, completed the
   correction, and returned a staged patch.
5. The workflow generated publication text, made one content-bound commit,
   non-force pushed it, updated the PR text, and resolved the fixed thread.
6. It posted the next idempotent review trigger, polled again, and completed
   when the configured no-findings evidence for that exact trigger was
   verified with no eligible threads.
7. The durable run reached `completed`; no manual replay, direct SQLite edit,
   or manual GitHub write was used to reach that state.

Observed active durations from the durable journal:

| Stage | Duration |
|---|---:|
| Prepare to explicit start | 74.4 s |
| First review trigger write | 3.5 s |
| First review polling until actionable evidence was detected | 2m 10.4s |
| Exact-session adjudication | 52.0 s |
| Local Cursor correction | 2m 5.3s |
| Publication generation | 51.0 s |
| Commit, push, PR update and thread resolution | 17.7 s total |
| Second review trigger write | 3.4 s |
| Final polling until no-findings completion was detected | 2m 10.8s |
| Explicit start to completed | 8m 35.5s |
| Prepare to completed | 9m 49.9s |

The system records detection time, not the bot's server-side response creation
time. The two polling intervals above therefore measure when the evidence
became observable to the worker, not a precise provider latency.

### Commands and evidence

The controller/operator used the documented read-safe `pr-review-v2` flow:

```text
ai_dev_loop config validate --repo <isolated parish checkout>
ai_dev_loop pr-review-v2 prepare <existing PR binding; exact session supplied out of band>
ai_dev_loop pr-review-v2 start <run-id>
ai_dev_loop pr-review-v2 status <run-id> --output json
ai_dev_loop pr-review-v2 history <run-id> --limit 100 --output json
```

`resume` and `abort` were not needed for the final accepted run. Earlier
controlled acceptance attempts and their retained local artifacts are not
acceptance evidence for the final run.

## Gate B correction context

The controlled acceptance work surfaced and corrected Gate B boundary issues
around carrier recovery after a Cursor usage-limit condition and Cursor-chat
creation ownership. The corresponding implementation plans are retained under
`archive/implementation-history/plans/` as Phase 16.8 Gate B records. The
final accepted run above completed after those targeted corrections.

## Deferred issues — explicitly out of Phase 16.9 scope

The following list is authoritative for this handoff:

- mutable effect-bound result commitment retargeting;
- aggregate publication-result size validation;
- legacy active-process metadata compatibility;
- race-safe collision handling for protected writes;
- exhaustive READ polling / restart / drift matrix;
- LOCAL recovery coverage for both publication and adjudication;
- in-flight MUTATING crash, lease and authority coverage;
- abort during every real external boundary;
- full privacy sentinel E2E through all subprocess paths;
- SourceRunOrigin production-assembled E2E; and
- stronger existing-PR agent-count and protected-artifact assertions.

See `PHASE_16_8_DEFERRED_ISSUES.md` for rationale and expected future work.
Phase 16.9 must not add tests or production changes for these items merely
because it touches adjacent code. If a deferred condition occurs repeatedly,
create a separately approved future resilience plan.

## Phase 16.9 entry conditions and guardrails

Phase 16.9 may begin with this explicit scope:

1. Route the public `pr-review` command to the accepted v2 implementation.
2. Remove legacy PR-review engine, recovery-only paths, schemas, tests and
   unused helpers only after proving no v2 consumer remains.
3. Preserve the Phase 16.1 A/B barrier and all v2 safety contracts: reducer
   purity, SQLite authority, explicit `start`, protected artifacts, exact
   session/chat continuity, no `--last`, non-force Git operations and
   privacy-safe status/history output.
4. Do not add a v1 migration or legacy read adapter unless the user explicitly
   requests one.
5. Do not alter the accepted live PR, branch, original dirty checkout, or
   retained acceptance artifacts during the cutover.
6. Update README, MkDocs, CLI reference and troubleshooting so only the
   intended v2 public workflow is documented after cutover.
7. Run the full relevant suite, packaging/docs validation and the Phase 16.1
   barrier after shared-code changes.
8. Establish a normally reviewed committed Phase 16.8 baseline before changing
   Phase 16.9 code. This handoff does not commit, push or stage current work.

## Repository-state note

At handoff creation, Gate B hardening/correction changes remain staged and
`PHASE_16_8_DEFERRED_ISSUES.md` is a new untracked debt register. Neither is
modified by this handoff. Normal review and commit governance must settle that
baseline before the Phase 16.9 implementation begins.

## Open questions

None for Phase 16.9 cutover. Deferred resilience work is intentionally a
future prioritization decision, not an implicit Phase 16.9 requirement.
