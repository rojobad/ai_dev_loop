# Phase 16.4 Durable Engine - Normative Clarifications

Status: normative companion to
`phase-16-4-pr-review-v2-durable-engine.md` before approval.

These clarifications resolve two implementation details in the main plan. Cursor must
read and apply them together with the main plan. If wording conflicts, this companion
is authoritative for the points below.

## Fenced event ingress

- Generic `PrReviewEngine.apply_event()` must reject the effect-result event classes
  `EffectSucceeded`, `EffectRetryableFailure`, `EffectBlocked`, and
  `WriteOutcomeUncertain`. They enter only through a currently fenced
  `complete_claim()` call or the explicit expired-claim recovery path.
- `RetryDue` enters only through the durable timer service.
- `AbortRequested` enters only through `abort_run()`.
- Fatal/internal failure uses a named engine method that applies the typed event and
  performs terminal cleanup; it is not an unrestricted raw-event bypass.
- Every accepted transition into `completed`, `failed`, or `aborted` cancels or
  supersedes remaining live dispatches and timers and invalidates the active lease in
  the same transaction, while preserving lease generation/history.

## Rejected completion status

- Remove `result_rejected` from the minimum `pr_review_effects.status` vocabulary in
  the main plan. The minimum vocabulary is:

  ```text
  pending, claimed, succeeded, retry_wait, uncertain,
  blocked, cancelled, superseded
  ```

- When a correctly fenced completion reaches the reducer but the reducer returns
  `TransitionRejected`, journal the safe rejection and leave the dispatch `claimed`.
  Do not change snapshot/version/timers/outbox and do not convert the result to
  success.
- The same current worker may submit a new uniquely identified corrected typed result
  while claim and lease remain valid. If the lease expires first, apply the main
  plan's conservative recovery policy.

## Initial-state boundary

- `create_run()` accepts only a validated `PreparedState` whose embedded `run_id`
  exactly equals the requested run ID. Other state variants are loaded only from the
  durable database after reducer-driven transitions; they are not valid bootstrap
  inputs.

## OpenQuestions

None.
