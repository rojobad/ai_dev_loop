# Deferred Technical Debt — Phase 16.8

Status: consciously deferred on 2026-07-27. These items do not block the
controlled Gate B happy-path acceptance. They are required follow-up work before
the Phase 16.9 cutover or any broader production rollout.

## Existing-PR preimage hardening

### Preserve title bytes exactly

The adopted-existing-PR preimage currently trims leading/trailing whitespace
from the title while later authorization compares the live title exactly. A PR
whose title intentionally contains edge whitespace can therefore fail closed
despite no human change.

Follow-up: retain the original title verbatim; use trimmed text only to reject
an all-whitespace title. Add unchanged-whitespace and whitespace-drift tests.

### Verify the prepare-time head SHA at gateway authorization

The adopted preimage stores its prepare-time head SHA, but the write gateway
currently verifies repository, PR number, and branches without comparing that
stored SHA to an immutable origin/effect commitment. The normal happy path is
protected by the run-local hash-verified artifact reference; this remains a
defense-in-depth binding gap.

Follow-up: carry a minimal immutable original-head commitment into the first
update effect, validate it against the existing-PR origin, and require it to
match the protected preimage artifact before authorization. Do not compare it
to the post-push head SHA. Add a wrong-head artifact test that proves no write.

## Acceptance-resilience matrix depth

The Phase 16.8 happy path, common recovery paths, protected artifacts, and
core fencing are covered. The following exhaustive production-boundary matrix
work was intentionally deferred because it is expensive and uncommon relative
to the current acceptance objective:

- Complete READ fault/drift coverage across 5xx, auth, contradictory evidence,
  timers, journal persistence, and proof that no downstream work is dispatched.
- Effect-specific LOCAL lease-loss, expired-resume, and persist-before-complete
  coverage for adjudication as well as publication.
- In-flight production-ledger stale-authority, lease-loss, and abort coverage
  for each mutating effect, replacing remaining mock-gateway stand-ins.
- Abort coverage using real in-flight READ, publication, adjudication, Cursor,
  mutation, and reconciliation boundaries instead of timing/sleep stand-ins.
- A fully production-assembled, multi-round source-run *and* existing-PR
  workflow that asserts exact journal, outbox, timer, hash, and write counts.

Follow-up: schedule these as a dedicated resilience phase with bounded fake
process/network boundaries and persistent SQLite reopen coverage. They must not
relax at-most-once mutation, reconciliation, lease fencing, or abort ordering.

## Privacy process-boundary expansion

Current privacy checks cover protected artifacts and operational surfaces, but
some sentinel paths still rely on fake Codex runners rather than a single
end-to-end injected process carrying stdin, stdout, and stderr through all
LOCAL/Codex/supervisor producers.

Follow-up: add that process-boundary test with synthetic sentinels only; scan
safe operational surfaces and preserve raw content solely in owner-only,
hash-verified artifacts.

## No-findings bot-presentation tolerance

The no-findings completion rule deliberately remains fail-closed: it requires
configured accepted comment prefixes or an allowlisted `+1` reaction on the
exact trigger comment. A future bot wording/presentation change can leave a
run waiting even when the human-visible bot comment says it found no issues.

Follow-up: only after collecting stable real examples, consider a narrowly
specified, content-bound no-findings evidence rule. It must retain actor,
exact-trigger, timestamp, reviewed-commit, and eligible-thread safeguards; it
must never become free-form semantic acceptance of arbitrary bot prose.

## Disposition

These entries are documented residual risk, not authorization to edit run
state, protected artifacts, GitHub comments, or historic acceptance evidence.
Gate B must still use a fresh `prepare` after the existing-PR preimage feature
lands; old runs without that ref remain fail-closed by design.
