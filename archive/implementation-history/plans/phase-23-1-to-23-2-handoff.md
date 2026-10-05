# Phase 23.1 → revised 23.2 handoff

## Operator decision and preserved baseline

On 2026-10-05 the operator stopped sequence `ai-dev-loop-seq-73fdcc91612f`
and run `ai-dev-loop-3b19276bb80a` while Cursor was correcting for review 7.
The scheduler confirmed `aborted`, the owned process terminated, no pending
termination and no remaining capacity holder. Later phases never materialized.

The operator then authorized committing partial 23.1, setting Cursor to
`grok-4.7-high`, and launching revised 23.2–23.4 with `gpt-6.1-sol / high`
reviewers. Preservation commit:
`c443d8be8c71e6862bd3e806d051978602b0153d`, branch `codex/phase-23-sequence`,
checkout `/home/rojobad/Projects/ai_dev_loop-phase23-sequence`.
Its 51 changed/new files preserve the stopped implementation and fake historical
capture byte for byte. Captured Git-status artifacts retain their authenticated
final blank lines. No acceptance, merge or runtime installation is implied.

The original combined worktree at
`/home/rojobad/Projects/ai_dev_loop-phase23-execution` remains separate reference
material. Do not copy that patch wholesale or alter its Git/private state.

## Last completed review and interrupted work

| Review | Actionable findings | Independently reported tests |
| --- | --- | --- |
| 1 | 10: six P1, four P2 | Failed |
| 2 | 6: four P1, two P2 | Skipped because findings remained |
| 3 | 4: two P1, two P2 | Failed |
| 4 | 3: two P1, one P2 | Skipped because findings remained |
| 5 | 2: one P1, one P2 | Skipped because findings remained |
| 6 | 2: one P1, one P2 | Failed |

Review 6 independently ran the phase group (28 passed, one skipped), lint,
types and strict docs successfully. Isolated production-entry probes reported
one pass and four failures. Full-suite success in the executor report was not
independently revalidated in that review and does not close those failures.

The interrupted seventh correction changed evidence ancestry and added tests
and a captured ledger/artifact bundle. Inspect these committed changes before
editing; neither their presence nor the earlier reviewer report proves current
closure. The new run starts a fresh chat and reviewer; original private prompts,
reviewer bindings and run artifacts remain immutable.

## Inherited F-04 — P1: correction ancestry and exact authentication

The last reviewed analyzer selected inherited chat proof through staging
ownership. After a review-recovery successor produced local staging and another
failed correction, intact chat ancestry was rejected as insufficient. It also
compared reviewer prefixes and omitted exact selected-review iteration checks.
Hash-consistent changes to the resumed session suffix or review iteration were
incorrectly accepted by inspection.

Resolve admission/chat, staging, fix and B proof with their own authenticated
owners. An intact later successor correction must authenticate; full-identity
or iteration mismatches must reject through the read-only production entry.
These are evidence-inspection guarantees. Forced correction recovery remains
23.3 scope. Add independent regressions even if the inherited partial fix now
passes the reported scenarios.

## Inherited F-09 — P2: mandatory coverage and authentic historical replay

The last reviewed fixture retained events but did not replay their payloads,
digests and associated attempts/effects/outcomes. Timestamp ordering, unrelated
later attempts, native sequence inspection eligibility/rejection, extended
correction CLI ceilings and actual bootstrap-proof corruption lacked complete
acceptance evidence. Some ordinary tests generated missing committed fixtures.

The inherited capture now includes a fake ledger and artifact bundle; verify
writer provenance and completeness, replay the captured data through production
inspection, and finish the full E-01–E-04 matrix. Tests must fail on missing
fixtures, preserve their bytes and run only on isolated copies, including SQLite
sidecars. Do not replace authenticity checks with test-helper state repairs.

## New acceptance boundary and launch configuration

[Revised 23.2](phase-23-2-initial-standalone-cursor-recovery.md) contract I-00
closes all E-01–E-04 and both inherited findings before I-01–I-04 forced initial
recovery work. The normal fresh reviewer evaluates the combined deliverable.
Committed old source is required production context for staged regression tests
and new publication callers; its preservation commit is not a waiver of defects.
No scheduler acceptance record for the aborted 23.1 is fabricated.

23.3 requires accepted revised 23.2 including I-00; 23.4 requires that combined
acceptance and accepted 23.3. Both retain their original mutation boundaries.
The [new sequence manifest](phase-23-2-4-grok-sequence.yaml) explicitly freezes
Cursor `grok-4.7-high` and reviewer `gpt-6.1-sol / high` for all three runs.
Seven completed reviews and 90-minute attempt limits remain unchanged. The YAML
change was directly authorized by the operator and is outside Cursor's scope.
Final manual review remains planned after the three automated acceptances.

## Updated validation policy on 2026-10-05

The operative 23.2–23.4 plans now require focused contract/regression checks.
Historical full-suite results above describe earlier execution, not a requirement
for the replacement run. E-01–E-04 coverage remains mandatory under I-00; do not
inherit the original 23.1 plan's generic repository-wide pytest command. The full
suite belongs to the separate post-sequence integration gate in the overview.
See [the 23.2 relaunch handoff](phase-23-2-focused-validation-relaunch-handoff.md)
for the unaccepted implementation and unfinished correction preserved next.
