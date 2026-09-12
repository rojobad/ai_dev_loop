# Phase 20.1.1 — Retryable Codex Reviewer Failures

## Goal

Prevent an authenticated, read-only Codex reviewer process failure from
stranding an already-completed Cursor implementation and its exact staged
patch.

When direct structured evidence does not identify a Codex usage limit, make one
bounded account-capacity observation before classifying the failure. If any
applicable primary or secondary limit window is exhausted, use the existing
`waiting_codex_capacity` continuation. Otherwise preserve a durable manual
review-retry checkpoint instead of irreversibly blocking the run.

Add an explicit `scheduler review retry <run-id>` operator action that retries
only Codex review against the exact protected staged snapshot, reusing the exact
bound reviewer session and never rerunning Cursor. It must also recover eligible
historical scheduler runs that were terminally blocked after a completed Cursor
turn because an older version classified a message-only usage-limit failure as
`codex_review_outcome_invalid`.

The motivating acceptance case is run
`ai-dev-loop-20260912T181533Z-a00705`: Cursor completed iteration 1 and its
staged patch was captured; the bootstrap reviewer identity was durably bound;
Codex then exited nonzero after emitting message-only usage-limit events and no
review result. The implementation must support this shape through hermetic
fixtures. Automated tests must not read or mutate the real run.

## Non-Goals

- Do not infer quota from arbitrary prose, generic exit codes, `last_error`, or
  missing review output alone.
- Do not automatically retry generic reviewer failures. Only a confirmed or
  capacity-inferred quota wait resumes automatically after capacity returns.
- Do not retry Cursor, create a replacement Cursor chat, bootstrap a second
  reviewer, use `--last`, change reviewer model/reasoning, or rewrite findings.
- Do not make identity ambiguity, artifact corruption, digest mismatch,
  dispatch mismatch, sandbox violations, or repository drift retryable.
- Do not commit, push, reset, clean, stash, unstage, apply patches, or otherwise
  rewrite the target repository.
- Do not implement Phase 20.2 sequence start/materialization, Phase 20.3
  checkpoint commits, or Phase 20.4 sequence reporting/abort behavior.
- Do not redeem credits, predict reset timestamps, change accounts, update
  Codex, or modify real timer/systemd/global integration state.

## Scope

- Extend the existing strict Codex failure classification with a bounded
  fallback capacity observation made only after authenticated reviewer
  completion evidence establishes an operational review failure.
- Add a typed durable state for a retryable Codex review failure that retains
  the complete post-staging checkpoint and repository reservation.
- Add typed events/reducers/store/schema/projection/history support for review
  failure, manual retry request, and retry scheduling.
- Add `scheduler review retry <run-id>` with stable text/JSON output,
  idempotent replay, exact eligibility checks, and safe next actions.
- Retry the same review iteration through the ordinary fenced Codex effect and
  attempt path, using the exact bound reviewer B and an attempt-unique result
  artifact path. Failed attempts do not consume a completed review iteration.
- Add a compatibility recovery path for eligible pre-Phase-20.1.1 terminal
  `blocked` scheduler runs by creating a distinct immutable-source review
  successor; do not transition the terminal source back to a nonterminal state.
- Update current CLI/troubleshooting/observability documentation and write a
  Phase 20.1.1 findings artifact.

## Out of Scope

- `ai_dev_loop.yaml`, package-owned Codex skills, hook/session bridge code,
  timer assets, global installation, credentials, and account mutation.
- Existing Phase 20.2 staged work in another worktree and the real protected
  artifacts for its blocked run.
- Generic retry commands for Cursor, Git admission, staging, capacity-probe
  protocol corruption, sequence checkpoint commits, PR review, or GitHub.
- Changing the existing structured `usage_limit_exceeded` classifier into fuzzy
  message matching.
- Automatically abandoning or deleting a retryable checkpoint.

## Required Context

Before editing, read:

- `AGENTS.md` and every `.cursor/rules/*.mdc` file.
- Phase 19 plan/findings and its strict usage classifier, capacity probe,
  `waiting_codex_capacity` state, reviewer-binding order, and tick behavior.
- Phase 17.5, 17.6, 17.8, 17.10, Phase 18, and Phase 20.1 plans/findings for
  Codex attempts, abort/fencing, timeout bounds, exact identity, controller-
  optional submission, SQLite authority, reservations, and protected artifacts.
- Current CLI and docs for `scheduler submit/start/tick/status/history/timeline`
  and the explicit absence of public scheduler recovery.
- `scheduler/codex_attempt_runner.py`, `runners/codex_failure.py`,
  `response_schema.py`, `application/codex_workflow_service.py`,
  `application/codex_evidence.py`, `application/codex_capacity_probe.py`,
  `application/attempt_service.py`, `application/tick.py`,
  `application/contracts.py`, `application/history.py`, and safe-action logic.
- Scheduler domain state/events/reducer/common/codex contracts, SQLite store and
  migrations, protected artifact/path helpers, status/controller projections,
  and schema tests.
- The fake Codex/Cursor fixtures and Phase 17.5/17.6/18/19 scheduler tests,
  including process failures, capacity continuation, idempotent effects,
  repository reservations, abort precedence, and privacy assertions.

## Cursor Rules And Skills

- Follow `AGENTS.md` and all repository-local Cursor rules:
  `ai-dev-loop-governance.mdc`, `ai-dev-loop-orchestrator-contracts.mdc`,
  `ai-dev-loop-state-and-schema-contracts.mdc`,
  `ai-dev-loop-codex-review-contracts.mdc`,
  `ai-dev-loop-loop-and-resume-contracts.mdc`,
  `ai-dev-loop-abort-contracts.mdc`,
  `ai-dev-loop-docs-acceptance-contracts.mdc`, and
  `ai-dev-loop-global-integrations-contracts.mdc`.
- The repository has no `.cursor/skills` directory. Do not modify or invoke the
  package-owned `ai-dev-loop-controller` or `ai-dev-loop-handoff` assets.
- Use only fake agents, temporary repositories, temporary XDG/config homes,
  injected clocks/IDs, and injected probe/process ports in tests.
- Leave implementation changes unstaged and uncommitted for independent review.

## Architecture Guardrails

### Failure classification and capacity fallback

- Preserve structured `usage_limit_exceeded` as the strongest quota evidence.
  Do not loosen it into general prose matching.
- Consider the fallback probe only after the attempt envelope, dispatch,
  effect kind, run/attempt identity, and protected output bindings authenticate,
  and after one exact reviewer B identity is already durably bound.
- Do not probe for artifact/digest/identity/sandbox/dispatch corruption or an
  ambiguous/unbound bootstrap. Those remain hard blocks.
- For an otherwise operational Codex failure—nonzero exit, timeout, missing or
  invalid result, or schema-invalid result—perform at most one bounded capacity
  probe during ingestion. Reuse the Phase 19 adapter and the frozen Codex
  command/environment policy; do not introduce another account transport.
- `EXHAUSTED` means at least one applicable validated primary/secondary window
  has zero remaining capacity. Transition to `waiting_codex_capacity` and
  record only a safe evidence-source code such as `structured_error` or
  `post_failure_capacity_probe`; never persist percentages, reset timestamps,
  account identity, raw protocol, or provider prose.
- `AVAILABLE` means the failure is not classified as quota and becomes manually
  retryable. `UNAVAILABLE` also becomes manually retryable with a safe
  diagnostic; lack of telemetry must not fabricate quota or destroy a valid
  review checkpoint.
- A false quota inference is allowed to wait and make one later review attempt
  after capacity returns. If that attempt fails for the original reason while
  capacity is available, it becomes manually retryable rather than looping.

### Retryable review checkpoint

- Add a specifically named nonterminal state such as
  `waiting_codex_review_retry`. It must retain the admitted checkpoint, exact
  Cursor chat, exact bound reviewer B, current review iteration, staged patch
  path/hash, frozen plan/prompt/config/runtime, last completed attempt identity,
  and a concise typed failure code.
- Keep the repository reservation while waiting for an operator retry. No other
  run may claim or mutate this worktree. `scheduler abort` remains the explicit
  way to end the wait and release ownership without changing repository files.
- Do not count a failed reviewer process as a completed review iteration.
  `max_review_iterations` counts only schema-valid Codex decisions.
- Status/history/controller output may expose the retryable state, iteration,
  safe failure kind, and retry command only. Never expose provider prose, raw
  JSONL, prompts, patches, full IDs, review content, or account telemetry.

### Manual retry command

- `scheduler review retry <run-id>` is the sole explicit authorization for a
  generic reviewer retry. It must be process-free: persist intent/state/effect
  eligibility and let a later scheduler tick launch the attempt.
- Repeated invocation for the same failure generation is idempotent and returns
  the already-scheduled/current retry. A subsequent failed attempt creates a new
  generation that requires another explicit operator action.
- Before scheduling, authenticate the durable checkpoint and verify through the
  existing Git adapter that repository root/common-dir/git-dir, branch, HEAD,
  frozen plan/prompt bindings, and the complete current staged patch still
  match. Reject tracked unstaged changes and untracked non-ignored files.
- Never normalize staging, write the worktree, or repair drift. Verification is
  read-only and fail-closed.
- Resume the exact bound reviewer B using the submission-frozen model and
  reasoning effort. Never bootstrap another B. Use the ordinary global capacity
  claim, effect dispatch, systemd attempt, abort, timeout, and ingestion paths.
- The retry instruction may add only a deterministic operational envelope that
  says the prior attempt did not yield a valid structured result and requests a
  fresh schema-valid review of the same staged snapshot. It must not summarize
  code, invent findings, or modify the original review inputs.
- Use attempt-unique Codex result/metadata/report paths so malformed or partial
  artifacts from a failed attempt remain immutable and can never be overwritten.
  The accepted review event/state points to the exact successful artifact.

### Compatibility recovery for the motivating blocked run

- Terminal source runs remain terminal and immutable. For an eligible older
  `blocked` source, `scheduler review retry` creates or idempotently reuses a
  distinct review-recovery successor and returns its run ID.
- Eligibility must be reconstructed from authenticated ledger records and
  protected artifacts, never from `block_reason_summary` prose alone. Require:
  completed Cursor turn and staging event; exact staged patch artifact/hash;
  authenticated completed Codex attempt; durably bound reviewer B; no valid
  review decision; no active process/effect; matching repository identity,
  branch/HEAD/plan/prompt/config; and an allowlisted operational review block
  kind such as `codex_review_outcome_invalid`, timeout, truncation, missing
  result, or schema-invalid result.
- Reject bootstrap uncertainty, absent reviewer identity, invocation/artifact
  integrity failures, identity conflicts, repository drift, missing checkpoint
  evidence, or a source that already has a review-recovery successor.
- The successor has a new run ID, immutable lineage to source run/iteration/
  patch/attempt, its own protected artifact root, and a reservation transferred
  or reacquired atomically. Copy only bounded verified artifacts required for
  deterministic continuation; never symlink or edit the source root.
- The successor starts at the same review scheduling checkpoint and first
  resumes B. It does not run admission, create a Cursor chat, or execute the
  completed Cursor turn again. If Codex later returns findings, the successor
  may continue the normal correction loop using the exact recovered Cursor chat.
- Prove with a hermetic fixture matching the motivating run shape that the
  successor reviews the existing staged patch and can reach `completed` or
  `completed_with_residual_risk` without any Cursor invocation before review.

### Persistence, concurrency, and safety

- Add a forward-only SQLite migration and backward-compatible typed/schema
  strategy. Historical snapshots/events remain readable without reinterpretation.
- Make state transition, event append, retry generation, effect availability,
  reservation ownership, and successor lineage changes transactional with
  version/CAS and uniqueness constraints. A crash/replay cannot duplicate an
  attempt or successor.
- Preserve tick lease, capacity claim, attempt ownership, abort precedence, and
  systemd fencing. Never launch directly from the CLI command.
- Treat agent output and account telemetry as untrusted/sensitive. Persist raw
  diagnostics only in protected artifacts with user-only permissions.

## Implementation Plan

1. Characterize the real message-only limit failure with a hermetic Codex JSONL
   fixture and prove why the strict classifier returns unknown while the
   capacity probe returns exhausted. Add regression tests before workflow edits.
2. Refactor Codex ingestion into authenticated integrity failures versus
   operational review failures. Reuse one bounded post-failure capacity probe
   only for the latter and route exhausted observations through the existing
   Phase 19 wait/resume path.
3. Add the retryable review state, events, reducers, migration/store support,
   schemas, status/history/controller projections, reservation retention, and
   abort behavior.
4. Make Codex review result/report/metadata artifacts attempt-unique and update
   readers/events to bind the exact accepted artifact while keeping historical
   fixed-path runs readable.
5. Implement the `scheduler review retry` CLI/application service for the
   nonterminal retryable state with read-only repository/patch verification,
   idempotent retry generations, and ordinary effect scheduling.
6. Implement immutable-source compatibility recovery for eligible historical
   blocked review checkpoints, including bounded artifact copying, lineage,
   reservation acquisition, idempotent successor reuse, and direct continuation
   at Codex review without Cursor replay.
7. Add integration tests for direct quota evidence, inferred quota, false quota
   inference, available/unavailable probe results, repeated manual failures,
   same-B resume, current-run-shaped successor recovery, drift rejection,
   concurrency, abort, privacy, and regressions.
8. Update only current behavior documentation and write
   `archive/implementation-history/findings/phase-20-1-1-reviewer-failure-recovery.md`.

## Testing Criteria

- **Classifier/probe unit tests:** strict structured quota remains recognized;
  message-only `error`/`turn.failed` remains unclassified by prose; authenticated
  failure plus exhausted primary or secondary window infers quota; available
  and unavailable observations become retryable; invalid probe data never
  becomes quota.
- **Failure-boundary tests:** dispatch/session/effect/digest/sandbox mismatches
  and unbound bootstrap remain hard blocks and never probe; nonzero exit,
  timeout, truncation, missing result, invalid JSON, and schema-invalid review
  with a bound B preserve retryable checkpoints.
- **State/schema/store tests:** new state/events/lineage validate in Pydantic and
  JSON Schema; migration preserves v5 data; state-kind queries/tick eligibility
  work; CAS/uniqueness prevent duplicate retry generations, effects, successors,
  or reservations; historical snapshots still load.
- **Manual retry tests:** command is process-free and idempotent; exact patch and
  repository identity are required; drift and dirty files fail closed; next tick
  resumes the same B and never creates B or Cursor; failed attempts do not
  consume review iterations; later valid findings use the normal correction loop.
- **Artifact tests:** each attempt uses distinct bounded protected paths;
  previous partial/invalid artifacts remain byte-identical; successful review
  binds the correct digest; no symlinks/cross-root escapes/overwrites.
- **Historical recovery tests:** a fixture matching the Phase 20.2 incident
  creates one successor, leaves the source row/events/artifacts unchanged,
  copies only verified necessities, reacquires the worktree reservation, skips
  admission/Cursor, resumes the exact B, and completes from the same staged hash.
- **False-positive test:** unrelated review failure plus exhausted capacity waits;
  after capacity becomes available one automatic review occurs; repeating the
  unrelated failure with capacity available stops in the manual retry state.
- **Abort/privacy tests:** abort from retry wait/successor preserves staged files
  and diagnostics, releases ownership only when safe, and never leaks raw events,
  provider messages, percentages, reset times, full IDs, prompts, patches, or
  review content through CLI/status/history/controller output.
- **Regression tests:** standalone submit/start/tick, Cursor corrections,
  Phase 19 repeated capacity waits, scheduler timer budget, controller-optional
  operation, reservation conflicts, and Phase 20.1 sequence prepare/status remain
  unchanged.

Use fake executables, injected probes, temporary repositories, and temporary
XDG roots only. Do not exercise the real motivating run or real account in tests.

## Validation

At minimum run:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_response_schema.py \
  tests/unit/scheduler/test_phase19_codex_capacity.py \
  tests/unit/scheduler/test_phase20_1_reviewer_retry.py \
  tests/unit/scheduler/test_schema.py \
  tests/unit/scheduler/test_tick.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/scheduler \
  tests/integration/test_phase17_5_scheduler_review_loop.py \
  tests/integration/test_phase20_1_reviewer_retry.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m build
uv run mkdocs build --strict
git diff --check
```

If a named new test file is split differently during implementation, preserve
the listed coverage and document the actual commands/results in findings.

## Risks Or Recovery Notes

Capacity is point-in-time evidence, not proof of causation. An unrelated failure
that coincides with exhausted capacity intentionally receives one delayed retry;
the design prevents an automatic generic-failure loop once capacity is available.

The highest-risk boundary is historical recovery: it must never trust the current
index merely because paths/status look similar. Exact protected hashes, reviewer
identity, repository identity, attempt evidence, and transactional reservation
ownership are mandatory. If any fact is unavailable, preserve the source and
require a fresh scheduler run or independent manual review.

This phase changes scheduler recovery and persisted state. Do not install the
implementation globally or apply it to the real blocked run during automated
execution/review. After independent acceptance and commit, the operator may
explicitly install it and invoke `scheduler review retry` against the real run.

## OpenQuestions

None.
