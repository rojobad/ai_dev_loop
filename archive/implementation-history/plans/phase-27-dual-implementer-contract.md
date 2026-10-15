# Phase 27 — Cursor or Codex implementation, with Codex capacity continuation

## Goal

Deliver a small complete release in three phases that can submit and execute
standalone runs and sequences using Cursor or Codex as implementer, while Codex
remains the independent reviewer. The initial implementation sequence itself
uses existing Cursor grok-4.7-high and reviewer gpt-6.1-sol/high. Later user runs
can explicitly select gpt-5.6-sol/xhigh as Codex implementer and gpt-6.1-sol/high
as reviewer. Model strings are passed exactly and verified against the installed
CLI at admission; do not silently substitute models or inherit CLI defaults.

## Approved Scope And Decisions

1. Retain Cursor and existing manifests/configs/runs. No historical bulk rename,
   rewritten frozen inputs or dependency on unimplemented Phase 24/25/26.
2. New public implementer configuration: optional YAML `implementer` mapping,
   `provider: cursor|codex`. Omitted mapping/provider retains Cursor behavior.
   Codex fields: `command` (default codex), mandatory explicit `model` and
   `reasoning_effort`, `timeout_minutes` (positive, default 90). Codex sandbox
   is workspace-write and noninteractive approval policy is explicit; no bypass.
   Cursor settings remain in `cursor`, with existing overrides/semantics.
3. Standalone CLI additions: --implementer-provider, --implementer-command,
   --implementer-model, --implementer-reasoning-effort,
   --implementer-timeout-minutes. Generic Codex fields are rejected for provider
   Cursor rather than silently ignored. Existing --cursor-* flags remain valid
   for Cursor; reject explicit conflicting Cursor overrides with Codex selection.
   Reviewer --codex-review-model/--codex-review-reasoning-effort stay independent
   and mandatory. Unknown providers, empty strings, explicit null mappings or
   fields, bool-as-int timeouts and unsupported effort values are rejected.
   Efforts use the existing supported Codex validation vocabulary, including
   high/xhigh; validate against current CLI/probes rather than inventing aliases.
4. Sequence manifest v2 adds per-phase `implementer`, using identical fields;
   v1 stays Cursor-only and readable/executable. v2 omission means Cursor.
   Prepare freezes the resolved implementer independently per phase. No
   mid-run/provider/model switching. New snapshots/bindings use explicit versions;
   old absent implementer metadata resolves only to historical Cursor, never Codex.
5. Codex implementer starts with codex exec --json and captures authenticated
   thread.started identity, then uses exact codex exec resume for corrections
   and capacity continuations. Reviewer B starts only at first review, separately.
   Persist purpose/role with identity; reject accidental identity collision.
   No --last, controller reuse, ephemeral session or shared implementer/reviewer.
6. Keep codex-review-result-v1 and its `cursor_fix_prompt` field unchanged; treat
   that legacy name as the exact implementer correction payload. Forward bytes
   unchanged through the execution envelope to the selected provider. Review
   wrapper labels the actual provider and includes prompt/final response without
   requiring access to XDG files. No schema/skill rewrite to make tests pass.
7. Capacity waiting is mandatory, not deferred: canonical state remains
   waiting_codex_capacity, with a versioned durable role/turn continuation.
   Historical waits absent role remain reviewer waits. Implementation waits do
   not require reviewer identity or a reviewed staged patch. Track same run,
   turn (initial/correction), exact prompt binding, authenticated session if any,
   frozen settings, wait generation and evidence. Reuse current bounded
   app-server capacity probe: EXHAUSTED or UNAVAILABLE retains wait; AVAILABLE
   authorizes one fenced retry of the pending role. No fixed 5h sleep or wall-clock
   deadline alone implies available capacity. Preserve all relevant capacity
   windows; if probe is model-agnostic retain current conservative semantics and
   document this limitation. Do not add a general quota/billing system.
8. Detect quota from protected contemporaneous structured/provider evidence and
   existing reliable post-failure probe strategy, not fuzzy public last_error.
   Repeated quota returns to wait with new attempt evidence. Neither waiting
   nor retries consume completed-review budget, finish a turn, advance phase,
   normalize staging or start reviewer prematurely. Preserve partial work;
   continuation never reruns clean-baseline admission or discards changes.
9. Before dispatch persist the durable attempt intent. Capture a first-turn
   session even if quota interrupts the turn. With exact identity, resume it.
   If authenticated quota evidence plus completed attempt evidence proves no
   session was created and no work ran, wait then retry fresh bootstrap once
   under the ordinary fence. Conflicting/missing identity after possible work,
   incomplete output or uncertain process termination blocks safely and retains
   reservation; never bootstrap a replacement. Abort fences pending work and
   stops only exact owned process; uncertain termination keeps the hold.
10. Generic failures/timeouts stop with classified diagnostics; full automatic
    timeout/recovery parity for Codex implementer is deferred. Existing
    cursor-retry/review-retry/recovery commands must reject Codex implementer
    contexts they do not support, without mutation. Review retry on an
    implementer capacity wait must not dispatch reviewer. Automatic capacity
    continuation itself works in this release without operator recovery commands.
11. Normal success follows existing staging/review/acceptance and sequence
    checkpoints. Final changes remain staged. Later phases may mix providers,
    each with an independent implementer session and reviewer session.
12. Status/history/timeline/integration summaries expose implementer provider,
    pending role/turn and safe capacity reason/next action, not full IDs or
    prompts. Keep old read-only historical inspection mutation-free. Existing
    explicit protected content reads must support new provider artifact routing
    where affected; unsupported recovery features are advertised honestly.

## Non-Goals

Complete recovery parity, automatic model/provider fallback, general provider
plugins, cost/account management, Cursor master/executor, dashboard, SDK migration,
or any new autonomous Git authority. No production installation as an agent/test
side effect. An installed release update after final validation is separate
operator work, not implicitly performed by this sequence.

## Phase Boundaries

27.1 delivers typed/versioned configuration and admission with execution safely
gated until 27.2; Cursor still works. 27.2 delivers standalone execution including
capacity waiting from initial implementation and correction. 27.3 delivers
sequence selection/continuation, inspection/docs and focused release acceptance.
The initial three-phase sequence remains v1/Cursor for all phases, so an already
installed scheduler can execute it without live provider switching or hot upgrade.

## OpenQuestions

None.
