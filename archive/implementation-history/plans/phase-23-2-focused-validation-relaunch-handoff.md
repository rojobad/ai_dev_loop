# Phase 23.2 — Preserved work and focused-validation relaunch

## Authorization and preserved state

On 2026-10-05 the operator authorized stopping the current sequence, preserving
its partial implementation and relaunching with focused subphase validation.
Native abort terminated the owned process, with no pending termination or capacity
holder. Sequence `ai-dev-loop-seq-0e70cf5705e5` and run
`ai-dev-loop-e3d0cf1723c9` are aborted; their frozen inputs remain historical.
23.3 and 23.4 were cancelled before materialization.

The clean admission baseline was
`f5e801b2d71a2ab08a7a14352776b0deff34d950`. All 29 changed/new files, including
unfinished correction changes, were preserved byte-for-byte in local commit
`aa33a6728c1d4b4e4e73d4dbad9f4116c7437832` on `codex/phase-23-sequence`.
This is unaccepted WIP. No merge, installation or scheduler acceptance is implied.
The new execution branch starts at the admission baseline plus the explicit
controller-authored validation policy and revised planning inputs.

## Restore partial implementation after admission

From the new execution checkout, the implementation agent restores the complete
preserved 23.2 patch into its review target:

```bash
git diff --binary f5e801b2d71a2ab08a7a14352776b0deff34d950 aa33a6728c1d4b4e4e73d4dbad9f4116c7437832 -- . | git apply --3way
```

Both commits are in the same local Git object store. This restores production,
tests and operational docs without changing HEAD or discarding files. Inspect
the resulting staged/worktree changes, retain the new focused validation policy
in any governance overlap, then complete the approved scope. A restoration
conflict is resolved deliberately; never reset/clean or discard preserved work.
Do not replay the original combined Phase 23 patch. Do not resume old agent
sessions or rewrite any old private scheduler artifact. The new run creates a new
implementation chat and reviewer. All restored changes undergo its staged review.

## First review and unfinished correction

The aborted run completed one review: seven findings, three P1 and four P2;
independent checks reported failed. Its subsequent correction was interrupted,
so none of the partial fixes has an accepted closure. Verify these against the
restored implementation and add independent regression evidence:

| ID | Level | Required closure |
| --- | --- | --- |
| F-11 | P1 | Apply initial-recovery launch authority only to its authorized initial retry. Later initial/correction usage-limit continuations must use their current authenticated envelope and complete with the same chat/reviewer. |
| F-12 | P1 | Reauthenticate complete causal failure evidence and current abort/hold/ownership eligibility at candidate insertion and pending-to-ready publication. Missing or corrupted evidence prevents ready publication and dispatch. |
| F-13 | P1 | Verify every v1 record binding against durable authority, including dispatch, frozen/admitted inputs, prompt derivation and parent provenance. Hash-consistent mismatches and absent/tampered parent records reject creation, replay and launch. |
| F-14 | P2 | Align record and publication-intent domain/schema required version, null/coercion, parent consistency, identifier and digest contracts. |
| F-15 | P2 | Handle expected missing/corrupt pending-publication evidence per relation, preserve its non-dispatchable ownership and expose a bounded diagnostic. Unrelated tick work progresses; intact restored evidence permits reconciliation. |
| F-09 | P2 | Exercise an actual unrelated attempt after the decisive failure through production inspection. Timestamp changes to existing earlier attempts do not prove this contract. |
| F-16 | P2 | Independently assert literal prompt bytes; preserve real staged/unstaged/untracked sentinels and index; use distinct publishers and deterministic force/abort/tick races; cover final-ledger interruption and replay. |

These summarize confirmed findings, not new phase scope. Inspect current partial
fixes before deciding what remains. Complete all E-01–E-04 and I-00–I-04, including
contracts beyond the seven findings, under the operative 23.2 plan.

## Validation and subsequent phases

Run mandatory subphase contract tests and affected regressions. First review
covers that focused selection; corrections rerun failed/affected checks and expand
only for a concrete remaining risk. No repository-wide pytest during development
or subphase review. The separate full-suite integration gate remains pending
until the combined sequence is accepted, before final manual acceptance.

23.3 and 23.4 require normal acceptance of the preceding revised runs. The
[focused manifest](phase-23-2-4-focused-sequence.yaml) retains Cursor
`grok-4.7-high`, reviewer `gpt-6.1-sol / high`, seven completed reviews and
90-minute attempt limits. Policy/planning/review-skill edits are authorized
operator control-plane work in the launch baseline, not Cursor implementation.
