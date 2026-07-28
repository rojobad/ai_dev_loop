# Phase 16.8 — Deferred follow-up issues

Status: recorded after the controlled Gate B acceptance cycle completed on
2026-07-28.

Phase 16.8 has demonstrated the existing-PR happy path: preparation, explicit
start, two external review cycles, one local correction, commit and non-force
push, PR update, thread resolution, and durable completion. The items below
were intentionally deferred so the project could advance to Phase 16.9. They
are residual engineering debt, not claims that the behavior is implemented or
accepted.

## Priority for a future resilience phase

### High — artifact commitment cannot be retargeted

Effect-bound cached results use a content-addressed JSON object plus a mutable
`*.commit` pointer. A pointer replacement can select another valid object in
the same directory. Replace this with an immutable, binding-authenticated
manifest or equivalent fail-closed scheme, and cover conflicting concurrent
persists and post-finalization pointer mutation.

### High — aggregate publication-size validation

The publication runner validates individual title/body fields, while the
persisted canonical publication artifact has a separate aggregate limit. A
combination of individually valid values can fail late at persistence. Validate
the exact canonical persisted artifact before returning from the runner and
test byte boundaries, including UTF-8 values.

### High — compatibility for legacy active-process metadata

New active-process records require process start-time and executable identity.
Older records may therefore fail to deserialize and be treated as absent.
Provide an explicit backward-compatible reader: legacy live or ambiguous
records must remain visible and fail closed for signaling/relaunch decisions.

### High — race-safe collision handling for protected writes

Some exclusive-write collision paths still use pathname reads/checks. Harden
them with descriptor-based, no-follow, bounded verification so a concurrent
symlink, FIFO, or replacement cannot be followed or chmodded.

## Deferred acceptance coverage

### READ polling and observation recovery matrix

Complete the production-boundary READ matrix for the exact claimed effect and
its artifact root: timeout, DNS/network failure, rate limits, representative
5xx responses, malformed data, identity drift, contradictions, durable retry
state, timer behavior, and restart before claim completion. The existing
happy-path acceptance covers normal polling only.

### LOCAL recovery matrix across both local effect kinds

Exercise lease loss, expired-claim resume, SQLite reopen after persistence and
completion fencing separately for publication generation and external
adjudication. Current coverage is stronger for publication than adjudication.

### MUTATING in-flight crash and authority matrix

Replace remaining mock-gateway rows with in-flight production-ledger coverage
for stale authority, lease loss, abort, ambiguous apply/timeout, reconciliation,
and at-most-once behavior for every Git/GitHub mutation kind.

### Abort during real external work

Exercise abort ordering against genuinely in-flight READ, publication,
adjudication, local Codex/Cursor, mutation, and reconciliation processes. The
remaining tests use lighter stand-ins for some of those boundaries.

### End-to-end privacy injection through subprocesses

Run all privacy sentinels through production fake Codex/Cursor subprocess
stdin/stdout/stderr paths in one persistent workflow, then verify that all
operational surfaces stay redacted while only owner-protected artifacts retain
the permitted raw evidence.

### Source-run origin end-to-end workflow

Add the counterpart production-assembled E2E beginning from a source-run
origin. It should cover the same completion conditions as the accepted
existing-PR flow, with exact journal/outbox/timer/hash/write counts.

### Stronger existing-PR evidence assertions

The accepted existing-PR Gate A harness verifies Codex process and supervisor
boundaries. A future pass should additionally assert the exact fake-agent
invocation count and explicitly read/hash-check every intermediate protected
artifact reference.

## Acceptance note

These items do not invalidate the completed controlled Gate B cycle. Before a
future broad rollout, multi-repository use, or resilience claim, they should be
planned and accepted explicitly rather than silently treated as complete.
