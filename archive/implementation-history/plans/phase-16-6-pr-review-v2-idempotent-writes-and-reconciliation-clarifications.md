# Phase 16.6 - Normative Write-Intent Clarifications

Status: normative companion to
`phase-16-6-pr-review-v2-idempotent-writes-and-reconciliation.md`

Date: 2026-07-21

This companion closes safety gaps found during the final cross-effect matrix review.
Cursor must read it together with the main Phase 16.6 plan. Where wording differs, this
companion is authoritative. It does not broaden Phase 16.6 beyond the seven existing
`MUTATING` effects and seven reconciliation strategies.

## Goals

- Persist the exact remote-ref precondition needed to reconcile an ambiguous push.
- Prevent public idempotency markers from exposing raw run/session identity.
- Bind PR/reply markers to the hash of their intended canonical content.
- Make protected input artifact formats explicit and versioned.
- Require a worker-supplied authority guard at the exact mutation boundary.

## Non-Goals

- Do not implement or reclassify `GeneratePublicationTextEffect` or another `LOCAL`
  effect; all remain Phase 16.7.
- Do not add a SQLite column/table, second intent log, public config/CLI, or legacy
  compatibility path.
- Do not relax any main-plan scope, privacy, fencing, validation, or testing rule.

## Scope

The main plan's pre-approved domain amendment is replaced by the following complete
list:

1. Add the expected branch to `CommitPatchEffect`.
2. Add `expected_remote_sha_before_push: GitSha40 | None` to
   `CommitRecordedOutcome` and `PushCommitEffect`.
3. Replace only the public `RequestBotReviewEffect.marker` value with a versioned
   opaque digest derived from the existing stable logical identity.
4. Keep all effect classifications, effect IDs, idempotency keys, reconciliation
   strategy membership, SQLite schema, and engine transaction protocol unchanged.

No other domain-shape or marker-semantic change is authorized without a plan amendment.

## Out of Scope

All main-plan exclusions remain in force. In particular, do not modify the Phase 16.5
read transport/gateway to add writes, and do not alter legacy publisher, GitHub,
configuration, lifecycle, recovery, A/B, CLI, control-plane, or SQLite migration files.

## Required Context

Read the main plan first, then this companion, then the current implementations of:

- `domain/common.py::build_effect_identity`;
- `domain/effects.py::{CommitPatchEffect,PushCommitEffect,RequestBotReviewEffect}`;
- `domain/events.py::CommitRecordedOutcome`;
- both initial and fix publication paths in `domain/reducer.py`;
- `application/contracts.py::EffectExecutor`;
- `workers/effect_worker.py`; and
- Phase 16.5 marker matching, artifact, lease, serialization, and variant-persistence
  tests.

## Cursor Rules And Skills

Follow every `alwaysApply` rule and the external staged-review ownership listed in the
main plan. This companion adds no rule or skill exception.

## Architecture Guardrails

### 1. Durable remote baseline

- The commit gateway must read the exact configured destination remote ref while it
  holds the repository lock, after the final authority check and before `git commit`.
- The read must authoritatively return one full SHA or absence. A temporary or malformed
  remote read is a pre-write retry/block and must prevent the commit.
- A present remote SHA must be an ancestor of `CommitPatchEffect.expected_head_sha`.
  A non-ancestor is remote/branch drift and blocks before commit.
- `CommitRecordedOutcome` carries that exact SHA or `None`; the reducer copies it into
  the successor `PushCommitEffect`. This existing event/effect payload is the durable
  precondition—do not create a sidecar checkpoint.
- Push preflight permits mutation only when the current remote ref still equals
  `expected_remote_sha_before_push`, including authoritative absence. The exact intended
  commit is idempotent success. Any third value is drift and performs no push.
- `FIND_REMOTE_REF` returns:
  - `APPLIED` only when the exact ref equals the intended commit;
  - `PROVEN_NOT_APPLIED` only when it still equals the durable baseline; and
  - `UNRESOLVED` for any other, incomplete, or contradictory evidence.

### 2. Opaque self-binding markers

- The trigger marker must no longer embed raw `run_id`. Derive a versioned public-safe
  marker from SHA-256 of the existing stable logical trigger identity. Phase 16.5 still
  matches the exact resulting marker and requires no transport semantic change.
- Derive PR-body and reply markers from operation kind, target kind, SHA-256 of
  `effect.idempotency_key`, and SHA-256 of the exact canonical unmarked content.
- Marker text must not contain raw run, session, claim, owner, generation, thread, or
  repository identity. The target remains bound by the typed effect and remote query.
- Reconciliation treats marker/content-hash mismatch, duplicate marker, or human-edited
  unowned content as `UNRESOLVED`, not safe absence.
- Commit messages use the main plan's opaque deterministic trailer plus independent
  expected-parent and patch/tree proof; a trailer alone never proves application.

### 3. Versioned protected inputs

- Define capped, strict, versioned readers for:
  - exact binary/UTF-8 staged patch bytes;
  - UTF-8 reply content;
  - publication title/body JSON; and
  - commit subject/body JSON.
- Use a repository-consistent schema name/version and document it in the Phase 16.7
  handoff so the later `LOCAL` artifact writer produces the exact accepted shape.
- Reject symlink/traversal, non-regular file, hash mismatch, unsupported schema/version,
  invalid UTF-8 where required, NUL/control abuse, empty required fields, or size caps
  before any mutation.
- Artifact text, patch bytes, reply bodies, publication text, and commit messages go by
  stdin or stay in memory; they never enter argv, safe errors, logs, journal, status, or
  SQLite.

### 4. Exact mutation authority guard

- `EffectWorker` must supply an authority guard/context to a specialized mutating
  executor. A generic executor without this contract cannot execute `MUTATING` work.
- After artifact/preflight reads and repository-lock acquisition, the mutating executor
  calls the guard immediately before each mutating process/API invocation.
- The engine-owned guard validates current lease owner/generation/expiry, claimed
  dispatch/claim, run version, effect identity, attempt, cycle, bound SHA, and abort/
  terminal state without exposing rows or a database connection.
- Guard rejection performs zero writes, marks authority lost for final fencing, and
  does not execute a replacement call. Never hold a SQLite transaction across the
  external/local process.
- Authority loss after process start remains ambiguous. The worker offers the eventual
  typed result once to `complete_claim()`; expired-claim recovery supplies the sole
  reconciliation route.

### 5. Stronger PR/text proof

- A pre-existing PR/body may be updated only after exact repository/head/base/full-SHA
  binding and complete candidate lookup.
- `PROVEN_NOT_APPLIED` for PR create/update or text update requires either complete
  authoritative absence or an intact v2-owned preimage whose marker and content hash
  prove that the new marker was not installed. Arbitrary human text without a valid
  owned preimage is `UNRESOLVED`, not authorization to overwrite.
- `APPLIED` requires one exact current marker whose embedded content hash matches the
  exact intended canonical title/body or reply artifact.

## Implementation Plan

1. Amend domain models/reducer/exports/tests with expected commit branch, durable
   pre-push remote baseline, and opaque trigger marker.
2. Freeze the versioned artifact DTOs and opaque self-binding marker helper before any
   gateway code.
3. Implement the authority-aware mutating executor contract and engine-owned read-only
   guard before enabling a write transport.
4. Capture the remote baseline inside commit preflight and carry it through the exact
   typed outcome/effect path.
5. Apply the clarified proof rules to push, PR, text, trigger, and reply execution and
   reconciliation.
6. Update the main plan's full unit/integration/fault/privacy matrices for every amended
   field and marker behavior.

## Testing Criteria

In addition to every main-plan test, automated coverage must prove:

1. The remote baseline round-trips through model serialization, SQLite variant
   persistence, accepted/duplicate receipts, reducer success, retry/resume, and crash
   recovery without a schema migration.
2. Push performs zero mutation when the current ref differs from the durable baseline;
   reconciliation classifies exact commit/baseline/third-SHA as
   `APPLIED`/`PROVEN_NOT_APPLIED`/`UNRESOLVED`.
3. Trigger, PR, and reply public markers contain no raw run/session/claim/owner/
   generation values and remain stable across attempt, restart, dispatch, and resume.
4. Marker/content-hash tampering and human-edited unowned PR text are unresolved and
   perform no overwrite.
5. The authority guard is called after lock/preflight and immediately before every one
   of the seven writes; rejection produces zero process/API calls.
6. A fake write that applies just as authority is lost is completed only through final
   fencing/recovery and is never immediately repeated.
7. Phase 16.5 observation still finds the new opaque exact trigger marker and all
   existing read/lease/privacy suites remain green.

## Validation

Run the complete focused, static, full-suite, docs, package, diff, and status commands
from the main plan. No additional real Git/GitHub/Cursor/Codex/model validation is
authorized.

## Risks Or Recovery Notes

- Capturing the remote baseline makes commit temporarily depend on a remote read. This
  is intentional: failure occurs before local mutation and prevents an unreconstructible
  push precondition.
- Existing Phase 16.3-16.5 persisted test variants must be updated atomically with the
  additive fields. No live/public v2 workflow or migration exists yet; nevertheless,
  corrupted/missing new fields fail closed rather than being guessed.
- Opaque marker changes invalidate only pre-production Phase 16.5 fixture expectations;
  no real trigger writer or accepted live v2 run exists.

## OpenQuestions

None.
