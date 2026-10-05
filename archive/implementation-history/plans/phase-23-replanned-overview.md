# Phase 23 — Manual Cursor recovery, replanned as four bounded runs

## Decision and accepted baseline

On 2026-10-04 the operator authorized stopping the combined Phase 23 run and
preparing four replacement plans. This overview supersedes the execution scope
of [the original plan](phase-23-manual-cursor-recovery.md). Keep that plan and its
original prompt unchanged as historical inputs; do not launch them again.

Planning baseline is accepted `main`, commit
`b516abdc7aa6582e17ad88a6c4ee94c3896499a9`. None of the replacement capabilities
below is implemented on that baseline. Each dependent plan requires its preceding
subphase to be accepted and available in the actual execution checkout.

### Stopped execution and preserved work

- Initial run `ai-dev-loop-20261004T215759Z-61a2c7` ended blocked after the
  `cursor.create_chat` timeout, before implementation or review.
- Replacement run `ai-dev-loop-20261004T234220Z-5e0e41` was explicitly aborted.
  Scheduler receipt: `state_kind=aborted`, `process_action=terminated`,
  `termination_pending=false`; subsequent controller inspection showed no
  active process/capacity holder and no next action.
- Review 1: ten findings, six P1 and four P2; tests reported failed.
- Review 2: nine findings, five P1 and four P2; tests reported failed.
  Publication/adoption, evidence ancestry, budget preservation, causal failure
  selection, compatibility, tests and documentation still needed correction.
- Preserved worktree: `/home/rojobad/Projects/ai_dev_loop-phase23-execution`;
  branch `codex/phase-23-execution`; HEAD
  `52b5c20b022697a9ef75903832ea0498722c7792`. There were 34 changed entries after
  termination, with staged, unstaged and untracked work retained. No acceptance,
  commit of that implementation, merge or installation is implied.
- Preservation fingerprints: staged binary diff SHA-256
  `5a4b9875e120c1b28fe31ce4490b8efbf08b28a22e7339d0efad1d833641841f`;
  unstaged binary diff SHA-256
  `dff1547fbc219d666608010f37935dde6345d877d6752cdfbb979c3ccf41453e`.

The stopped worktree is reference material. Implement each replacement against
the accepted baseline plus accepted predecessors; selectively adapt useful code
only after inspecting it against the new contract. Do not copy the combined patch
wholesale, use it as an assumed prerequisite, or resume its reviewer/chat.

## Order and observable value

| Run | Plan | Operator-visible result | Mutating recovery supported afterwards |
| --- | --- | --- | --- |
| 23.1 | [Eligibility and evidence inspection](phase-23-1-cursor-recovery-evidence.md) | Read-only `scheduler cursor-retry RUN_ID --check` reports authenticated evidence and explicit blockers. | None newly introduced. |
| 23.2 | [Initial standalone recovery](phase-23-2-initial-standalone-cursor-recovery.md) | `--force` creates one same-chat initial-turn successor, retaining partial work. | Initial standalone turns, including repeat recovery of failed initial successors. |
| 23.3 | [Correction and checkpoint continuity](phase-23-3-cursor-correction-continuity.md) | Failed standalone correction turns recover with exact B, review counters, authorized ceiling and evidence ancestry. | Initial and correction standalone turns. |
| 23.4 | [Original sequence continuation](phase-23-4-sequence-cursor-recovery.md) | A failed sequence leaf is replaced at the same ordinal, then ordinary review/checkpoint/next-phase flow resumes. | Eligible initial and correction turns, standalone and sequence. |

Each plan has a separate `prompt_phase-23-N-*.txt` file in this directory,
following the repository's intentionally ignored prompt convention. Planning
does not authorize preparing/submitting/starting this replacement sequence or
any run. Freeze explicit reviewer model/reasoning only when launching is approved.
Current repo configuration remains Composer 2.5, seven completed reviews maximum,
and 90-minute Cursor/Codex timeouts; this planning task changes none of those.

Handoff prompts: [23.1](prompt_phase-23-1-cursor-recovery-evidence.txt),
[23.2](prompt_phase-23-2-initial-standalone-cursor-recovery.txt),
[23.3](prompt_phase-23-3-cursor-correction-continuity.txt), and
[23.4](prompt_phase-23-4-sequence-cursor-recovery.txt).

The [four-phase sequence manifest](phase-23-replanned-sequence.yaml) records the
operator's proposed `gpt-6.1-sol / high` reviewer settings for all four phases.
Cursor and budgets inherit the current repository configuration. Preparing this
file does not submit or start a scheduler sequence. Each phase has a fresh
reviewer, normal automated acceptance and the predecessor's accepted checkpoint;
the final phase leaves staged changes for operator finalization. Starting the
sequence will authorize its existing non-final checkpoint commits.

## Contracts carried across every subphase

- Only an authenticated terminated failed `cursor.run_turn` causing
  `blocked(cursor_failure)` can be forced. Chat creation, preflight, integrity,
  cancellation, uncertain termination and active/waiting states are excluded.
- `--check` is inspection, not authorization or a lock. Mutating recovery must
  revalidate its evidence, current state, ownership and cancellation boundary.
  `--check` and `--force` are mutually exclusive once both exist.
- Keep no-flag timeout retry behavior and its exact prompt/output contract.
  Preserve usage-limit continuation and automatic retry policy.
- Preserve original blocked snapshots/journals/attempt artifacts. Successor
  authority lives in typed durable relations/intents and authenticated private
  artifacts. A unique source/failure relation makes repeats idempotent.
- Resume the same chat and frozen configuration. Append one fixed operational
  note to the exact failed invocation prompt; recognize an earlier manual envelope
  by authenticated provenance. Do not invent correction findings or rewrite B's
  instructions. Phase 23.2 defines the exact note and byte composition.
- Recovery authorization preserves index, staged/unstaged/untracked work and
  immutable inputs. A blocked failure may have released its reservation: recover
  ownership safely, refusing conflicts. No clean-baseline re-admission or Git
  rollback; normal staging occurs only after successful Cursor completion.
- Pending publication is non-dispatchable. A durable retry effect becomes
  runnable only after its private inputs, authority and required adoption are
  authenticated. Restart/abort callers must converge and retain ownership while
  publication or process outcome is uncertain.
- Preserve completed-review counts and authorized extensions without editing
  frozen submitted configuration. B is bootstrapped normally if genuinely absent;
  an existing B is resumed exactly. Recovery consumes no completed review.
- Every subphase includes its own model/schema compatibility, behavioral tests,
  operational docs and recovery guarantees. These are acceptance requirements
  when the behavior is introduced, not a final hardening phase.
- Use fake CLIs and isolated HOME/XDG/repos. Do not control the live hub sequence,
  call real models in tests, install this feature, change timers/lingering or
  modify `ai_dev_loop.yaml` or planning/review skills during implementation.

## Acceptance and reuse

Proceed in order after each subphase's automated acceptance. A final operator
review may examine all four runs' evidence; all per-run mandatory contracts and
checks must already pass. No new runtime gate, review budget policy, or automatic
abort rule is introduced by this overview. Repeated unresolved P1 invariants are
grounds for an operator to reconsider scope, rather than evidence of convergence.

Phase 23.4 is the first replacement that can address the original blocked hub
sequence use case. Real installation and recovery of that sequence require
separate explicit operator authorization after validation. Merely checking its
evidence in Phase 23.1 does not permit mutation or guarantee later eligibility.
