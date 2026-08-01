# Phase 16.12 — Deferred-reply dispatch recovery completion

## Goal

Close the Phase 16.11 recovery gap in PR-review v2: a recovered mixed adjudication
must complete its actionable local-fix/publication/resolution path, dispatch its
already-persisted deferred replies, and only then wait for the operator to confirm
the next external observation cycle.

In particular, make the current CryptoSentinel run
`prv2-16e2cd4c29eaec847405da3ae83d6174` recoverable from its durable current
shape: `waiting_for_user` with one valid pending `post_thread_reply` effect, no
live supervisor, no active lease, a new PR head, and a deferred context bound to
the pre-fix frozen snapshot.

## Non-Goals

- Do not redesign external adjudication, change its decision schema, or broaden
  Phase 16.11 beyond dispatching deferred replies correctly.
- Do not create a new public-write confirmation model. `pr-review start` remains
  the external-effects gate; this phase only resumes a pre-existing, validated
  effect after remediation.
- Do not auto-recover arbitrary paused, failed, claimed, stale, malformed, or
  legacy runs.
- Do not modify the current run's SQLite rows, historical events, artifacts,
  effects, GitHub data, target repository contents, or PR directly during
  implementation or validation.
- Do not change `ai_dev_loop.yaml`, installed skills, model selection, limits,
  Git staging behavior, generic A/B recovery, or unrelated Phase 16 work.

## Scope

- PR-review v2 domain predicates and supervisor stop/continue logic.
- PR-review control/status/resume guidance for a valid pending deferred-reply
  dispatch.
- Focused unit, integration, supervisor, and regression tests.
- User-facing PR-review CLI/operational documentation that describes the exact
  post-install recovery command and the no-write-versus-write boundary.

## Out of Scope

- Direct SQLite repair or manual mutation of protected artifacts.
- Re-adjudicating the current run again, fabricating a prompt/reply, or replacing
  its stored Cursor/Codex identities.
- Changing reconciliation semantics, GitHub adapters, the local-fix carrier, or
  publication content generation except where a test double is required.
- Reopening the intentionally deferred Phase 16.8 edge cases.

## Required Context

Read before editing:

- `archive/implementation-history/plans/phase-16-11-mixed-adjudication-actionable-priority-and-recovery.md`.
- `archive/implementation-history/findings/phase-16-8-to-16-9-handoff.md` for
  the v2 durability and external-effects contracts.
- `archive/implementation-history/master-plan.md`, the relevant current `docs/`,
  and every `.cursor/rules/*.mdc`.
- `src/ai_dev_loop/pr_review_v2/domain/state.py`, `domain/reducer.py`,
  `application/engine.py`, `application/control.py`,
  `workers/supervisor.py`, and `workers/effect_worker.py`.
- Existing Phase 16.11 tests, especially
  `tests/unit/pr_review_v2/test_phase16_11_mixed_adjudication.py` and
  `tests/integration/test_phase16_11_recovery_supersession.py`.

Observed failed trace to preserve in tests:

1. Legacy mixed `WaitingForUserState` has one reply plus two actionable decisions
   and no fix prompt.
2. `resume --recover-mixed-adjudication` supersedes only the old pending reply and
   emits a fresh `adjudicate_threads` effect. It makes no GitHub write.
3. Fresh mixed adjudication creates a real fix prompt, executes the local fix,
   publishes it, and resolves only actionable threads.
4. The resulting `WaitingForUserState` contains one pending, queue-head
   `post_thread_reply` bound to the new PR head. The supervisor currently exits
   solely because the state kind is `waiting_for_user`; `status` incorrectly
   advertises `--confirm-user-continuation`, whose reducer transition rejects
   non-empty `remaining_replies`.

## Cursor Rules And Skills

Follow all repository-local Cursor rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Relevant repository skills:

- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md` governs the later
  staged acceptance review; it is not implementation code.
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` applies to any
  operational documentation update.

## Architecture Guardrails

- Keep the reducer pure. It may derive deterministic state/effects but must never
  read SQLite, artifacts, GitHub, processes, or clocks outside its event input.
- SQLite snapshots, events, effect rows, leases, and protected artifacts remain
  authoritative. Do not migrate, overwrite, delete, or mutate historical rows to
  repair this run.
- A `WaitingForUserState` with a valid pending queue-head reply is dispatchable
  work, not an operator-continuation checkpoint. A `WaitingForUserState` with no
  active reply and no remaining replies is the only shape for
  `--confirm-user-continuation`.
- Centralize that distinction in a typed/domain-level predicate or equivalent
  single invariant used by supervisor exit, control/status guidance, and resume.
  Do not duplicate loose `active_effect is not None` checks across layers.
- A dispatchable reply must match the queue head, carry the current binding/head,
  and have a pending unclaimed durable effect. Claimed, retrying, stale, malformed,
  live-supervisor, or active-lease cases must fail closed or report `wait-until`.
- The supervisor may execute the already-persisted reply only after all local-fix
  publication and actionable-thread resolution effects completed. It must never
  emit a reply before remediation or construct a new reply/body.
- Ordinary `pr-review resume <run-id>` may repair/spawn the supervisor only for
  that exact validated pending-reply checkpoint. It must not append a user
  continuation event, duplicate an effect, bypass claim/lease fencing, or make
  any direct GitHub call itself.
- `--confirm-user-continuation` remains valid only after all deferred replies are
  durably complete; it then creates the next observation effect exactly as today.
- Status is a public safety contract: its advertised command must be accepted by
  `ControlPlaneService.resume` for the same durable snapshot, or status must say
  `wait-until`/`none`. Do not expose raw reply bodies, prompts, patches, tokens,
  session IDs, or SQLite contents.
- Preserve reply-only and all-actionable behavior, write retries/reconciliation,
  idempotency keys, head binding, abort/lease behavior, and the explicit
  `--recover-mixed-adjudication` no-public-write guarantee.
- Do not stage, commit, push, reset, clean, stash, unstage, or invoke real
  Cursor/Codex/GitHub during automated tests.

## Implementation Plan

1. Model the checkpoint distinction explicitly.

   Add or extend a typed, validated domain helper for the queue-head pending
   deferred-reply shape. It must verify `WaitingForUserState`, non-empty
   `remaining_replies`, a matching `PostThreadReplyEffect`, and the state/effect
   binding required for safe dispatch. Reuse existing effect-store status checks
   in the application/control layer rather than putting SQLite knowledge in the
   domain.

2. Correct supervisor lifecycle without changing the reducer's purity.

   Replace the unconditional `waiting_for_user` supervisor exit with a decision
   based on the centralized predicate/status: continue the normal claim/execute
   loop while a valid pending deferred reply exists; exit only when that state
   has no dispatchable reply and genuinely awaits operator continuation. Preserve
   existing terminal, paused, retry, timer, cancellation, lease, and bounded-step
   behavior.

3. Make control/status/resume agree with the executable state.

   For a stopped run with a validated pending queue-head deferred reply and no
   live supervisor/lease, make `status` advertise ordinary `resume` and mark it
   resumable. Make ordinary `resume` repair/spawn the supervisor idempotently,
   without emitting `UserContinuationRequested` or a new effect. If the reply is
   claimed, retrying, malformed, stale, or ownership is live, advertise
   `wait-until` and do not spawn.

   For `WaitingForUserState` with no remaining replies and no active effect,
   retain `resume --confirm-user-continuation`; only that command may append the
   continuation evidence/event. Calling confirmation before replies complete
   must remain rejected/fail-safe, but status must never suggest it. Keep legacy
   mixed recovery precedence only for its original unclaimed legacy shape.

4. Prove the full recovery trace, not only its first transition.

   Extend the Phase 16.11 fixture/harness to seed the exact cycle-3 legacy mixed
   snapshot, invoke recovery, deliver a valid fresh mixed adjudication result,
   drive the local-fix/publication/resolution effects with fake adapters, and
   reach the deferred reply state bound to the published head. Start/repair the
   supervisor through the public control boundary, let the fake GitHub executor
   complete the stored reply through normal claim/completion paths, then confirm
   continuation and prove the next bot-observation effect is scheduled.

   This test must use temporary XDG/SQLite/artifact roots and fake GitHub/Cursor/
   Codex processes or executors. It must not use the real CryptoSentinel
   repository, real GitHub, or real model activity.

5. Add contract and regression coverage around every boundary.

   Cover the supervisor's dispatch/exit decision; status-to-resume agreement;
   no duplicate post/claim on double resume; claimed/live-lease/live-supervisor
   refusal; retry/reconciliation preservation; reply-only and all-actionable
   regressions; stale/mismatched queue head rejection; deferred reply only after
   publication/resolution; and confirmation only after the final reply.

6. Update product documentation, not historical claims.

   Update the active PR-review CLI/operational documentation to distinguish:
   (a) `resume --recover-mixed-adjudication` as re-adjudication with no direct
   GitHub write; (b) ordinary `resume` as resuming an already persisted deferred
   reply after the write gate; and (c) `resume --confirm-user-continuation` only
   after replies are complete. Document that stale/claimed/live ownership blocks
   require `status`, not direct database repair. Do not rewrite historical phase
   handoffs.

## Testing Criteria

Automated tests are required. Use fakes/stubs for GitHub, Cursor, Codex, process
ownership, and time. Do not make network or model calls.

- Unit/domain (`tests/unit/pr_review_v2/test_phase16_11_mixed_adjudication.py`
  and targeted reducer/state tests): queue-head predicate accepts only valid
  pending reply shapes; post-reply completion clears the queue; confirmation
  before a reply remains rejected; confirmation after the final reply creates the
  next bot-review request; reply-only/all-actionable flows remain unchanged.
- Unit/control: for the same persisted snapshot, `status.next_action` and the
  accepted `resume` form agree. Pending queue head => ordinary `resume`; empty
  queue => `--confirm-user-continuation`; live/claimed/retry => `wait-until`.
  Assert rejected confirmation does not create a public-write effect or mutate
  the active durable effect.
- Supervisor/worker integration: a `waiting_for_user` state with a valid pending
  reply does not make the supervisor exit before `claim_next_effect`; it sends
  exactly one fake reply through normal claim/completion, respects effect retry
  and reconciliation semantics, releases its lease, and then exits awaiting the
  user only after the queue is empty.
- Full Phase 16.11 recovery regression
  (`tests/integration/test_phase16_11_recovery_supersession.py` or a dedicated
  sibling): exact legacy cycle-3 1-reply/2-actionable snapshot -> no-write
  recovery -> fresh mixed fix -> local fix -> publication -> actionable
  resolutions -> persisted deferred reply -> ordinary resume -> one reply ->
  user confirmation -> next observation. Assert immutable old rows, exactly one
  re-adjudication effect, no reply before remediation, stable effect IDs, current
  head binding, and no stuck `waiting_for_user` state with a pending queue head.
- Existing PR-control regression
  (`tests/integration/test_phase16_8_control_matrix.py`): retain all prior
  `waiting_for_user`, lease, supervisor, and command semantics unless explicitly
  changed above.
- CLI/docs tests: update help/status assertions when the safe next action changes;
  default output must remain redacted.

Run at least:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/pr_review_v2/test_phase16_11_mixed_adjudication.py \
  tests/integration/test_phase16_11_recovery_supersession.py \
  tests/integration/test_phase16_8_control_matrix.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/pr_review_v2
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

Run the full suite with native `/tmp` variables if the focused suites pass; if it
is environment-blocked, report the exact command, failure, and unaffected focused
evidence. Do not hide a failure behind a skipped test.

## Validation

Before considering this phase complete, Cursor must perform and report a concrete
in-memory/state-machine trace using the test harness, including each asserted
effect and state:

| Step | Required state/effect outcome |
| --- | --- |
| Legacy snapshot | Cycle 3, frozen IDs `(reply, actionable-1, actionable-2)`, pending queue-head reply, no fix prompt. |
| Recovery | Old reply superseded; exactly one deterministic `adjudicate_threads`; no GitHub executor call. |
| Re-adjudication | Fresh mixed evidence contains a protected fix prompt and exact actionable subset. |
| Remediation | Local fix, publication, and only actionable resolutions finish before any reply claim. |
| Deferred reply | `waiting_for_user` retains one valid pending reply bound to the published head; `status` says ordinary `resume`. |
| Supervisor repair | `resume` spawns/reuses one supervisor; it claims/completes exactly that persisted reply, with no duplicate effect. |
| Continuation | Queue becomes empty; `status` says `resume --confirm-user-continuation`; confirmation is accepted and schedules next observation. |

The test must fail against the current defective implementation and pass only when
the final row is reachable. It is not sufficient to assert that recovery reaches
`adjudicating` or that a reducer can be driven manually past the reply.

After staged review and local CLI installation, do not mutate the live run until
the controller sees its status advertise ordinary `resume`. With a separate
explicit user authorization for the external reply, the recovery command will be:

```bash
ai_dev_loop pr-review resume prv2-16e2cd4c29eaec847405da3ae83d6174
```

Then inspect status once. Only after replies are complete should a separate,
explicitly authorized `--confirm-user-continuation` be used.

## Risks Or Recovery Notes

The principal risk is confusing an operator-confirmation checkpoint with a
dispatchable durable write. The fix must therefore centralize the invariant,
preserve claim/lease fencing, and derive status from the same condition that
control accepts. A process crash after claim stays in existing retry or
reconciliation paths; this phase must not bypass them.

The existing live run remains intentionally untouched during implementation. Its
current pending effect is sufficient durable evidence; no migration is needed.
If validation cannot prove the exact trace, leave it waiting and report the gap
instead of attempting a database repair or a public write.

## OpenQuestions

None.
