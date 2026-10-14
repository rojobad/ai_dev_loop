# Phase 27.4–27.7 — Implementer recovery completion

## Goal

Complete the deferred Codex implementer timeout/failure recovery guarantees,
retaining Cursor, exact identities, capacity continuation and the existing Git
and sequence authority. Execute four phases with gpt-5.6-sol/xhigh implementer
and gpt-6.1-sol/high reviewer. This manifest is preparation input only: writing
these plans does not submit, start or authorize live workstation acceptance.

## Approved Public Contract

Add `scheduler implementer-retry <run-id> [--force] --output text|json`.
Cursor delegates to the existing cursor-retry services; cursor-retry remains a
compatible Cursor-only command. Codex no-flag retry authorizes a confirmed
terminated timeout continuation in the same run; --force authorizes recovery of
an eligible classified implementation/correction failure through one successor.
No timeout evidence means no no-flag retry. No --force on abort, capacity wait,
integrity failure, missing/conflicting identity, uncertain termination or other
unsupported blocked reason. Recovery requires an authenticated existing session;
failed bootstrap without identity is deliberately ineligible. Capacity waits
remain automatic through waiting_codex_capacity, with no invented manual bypass.

Match Cursor's existing timeout policy: at most three automatic retries per
logical turn, 30-minute spacing; explicit no-flag retry can advance the wait or
continue after exhaustion. Operational retries do not consume completed reviews.
Counters survive restart/capacity continuation and reset only for a genuinely new
initial/correction turn. Same session/model/effort/timeout, exact original prompt
bytes, and partial work remain intact. Persist a separate deterministic recovery
execution envelope if a note is necessary; never rewrite the raw correction.

Use versioned immutable recovery records, publication intents and exact source
attempt/session/prompt/config/patch bindings. A blocked failure source remains
immutable; explicit successor publication records durable user authorization.
Replays return the same successor. Do not run clean-baseline admission over
partial continuation. Retain repository identity and existing artifact-boundary
checks without continuous baseline policing or speculative Git drift gates.

Initial recovery carries implementation context; correction recovery also carries
reviewer identity, exact pending fix, completed-review count, effective ceiling
including explicit extensions and reviewed patch evidence. No extra B bootstrap,
no repetition of completed implementation/reviews, no --last or guessed sessions.
Keep reviewed artifact evidence historically verifiable. Authenticate partial work
only at the retry boundary according to existing recovery contracts.

Standalone failure recovery first; Phase 27.5 rejects sequence-leaf --force
without mutation until 27.6 supplies coordinated adoption. In 27.6 use the same
sequence/ordinal and current-leaf relation, never a new sequence or stale leaf.
Publication/adoption, reservation ownership, dispatch fencing, abort and checkpoint
handoff must converge after interruption. Pending successors are not dispatchable.

Do not toggle codexImplementerManualRecovery to true until initial/correction
standalone and current-leaf sequence force recovery are supported (27.6). Phase
27.4/27.5 advertise their narrower CLI capabilities honestly in help/docs.
Version public API changes according to the existing additive compatibility policy.

## Non-Goals

Model/provider switching, fallback providers, universal failure retries, a general
provider framework, live ledger repair, legacy command restoration, destructive
Git, automatic installation, account/billing tooling or Phase 24/25/26 work.

## Approval Record

The user approved scheduler implementer-retry, its no-flag/--force semantics,
and the three automatic timeout retries spaced 30 minutes apart on 2026-10-14.
The four plans and their implementation/reviewer model settings are authorized
for sequence preparation. Starting the sequence remains a separate action.
No further approval is required for ordinary implementation of this contract.
Real workstation validation/installation remains separate operator work.

## OpenQuestions

None.
