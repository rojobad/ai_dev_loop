# ai_dev_loop Agent Guidance

## Current workflow and source of truth

- New work uses `scheduler submit` -> `scheduler start` -> `scheduler tick`,
  or `scheduler sequence prepare` -> `scheduler sequence start` -> ticks.
  Read `docs/referencia/cli.md` for the supported command surface.
- Verify behavior against current code, schemas, CLI help, and tests. Keep
  rules and `/docs` aligned with that evidence. Archived plans and findings
  explain history; they do not reinstate retired commands or override the
  user's approved scope. Report unresolved safety decisions before dependent
  implementation; correcting an obsolete instruction is not such a decision.
- The central `engine.sqlite3` ledger and protected `artifacts/` are the
  scheduler's authority. Legacy `runs/.../state.json` is not live scheduler
  state. Do not restore top-level `prepare`, `start`, `resume`, `recover`,
  `launch`, or `pr-review` to satisfy historical instructions.

## Reviewer identity and frozen configuration

- Controller A is optional provenance. New submissions do not require a
  desktop session, a fork, or a message to another chat.
- `scheduler submit` requires explicit `--codex-review-model` and
  `--codex-review-reasoning-effort`. Sequence manifests supply both per phase.
  Freeze these inputs; do not inherit them from YAML defaults, controller
  session metadata, or Codex CLI configuration.
- Reviewer B does not exist at submission. The scheduler creates it once at
  the first review with `codex exec`, captures its exact identity, and uses
  `codex exec resume` for subsequent reviews and eligible retries. Never pass
  a pre-existing B through `--codex-session-id`, use `--last`, or guess an ID.
- The scheduler enforces `workspace-write` for the Codex subprocess. This
  capability does not authorize reviewer edits: the current wrapper and
  configured review skill require staged-only review without intentional
  repository modifications and permit relevant automated tests.
- Cursor remains the implementation agent and Codex the final reviewer.
  Provider switching and a scheduler-managed Cursor master/executor stage
  are not implemented configuration options.

## Product intent and Git authority

`ai_dev_loop` is an orchestrator for a durable conversation between a Codex
reviewer and a programming agent. Its purpose is to preserve their hand-offs,
decisions, reviewed artifacts, and explicitly approved Git actions -- not to
become a general-purpose controller of every worktree change.

- Use preflight as an admission check before an agent run begins. It may verify
  repository identity, frozen inputs, and a safe starting worktree, including
  rejecting an unexpectedly dirty baseline when the active workflow requires
  it.
- After admission, the reviewer and implementer own the technical decision
  loop. Do not add continuous baseline checks, exhaustive Git-status emulation,
  or speculative drift gates merely to constrain normal work performed during
  that exchange.
- Stage or commit only an explicit, durable decision approved by the agents and
  allowed by the active phase. Never infer approval, create an autonomous
  commit, or use destructive Git operations to force a desired state.
- Preserve the hard boundaries that make orchestration trustworthy: repository
  reservation, immutable input and artifact integrity, exact agent identity,
  explicit review decisions, and no destructive or external Git actions unless
  the approved plan expressly authorizes them.
- The supported runtime normalizes staging after Cursor. An explicitly started
  sequence also authorizes the existing local checkpoint commit for each
  accepted non-final phase. Standalone runs and final sequence phases leave
  changes staged; acceptance does not authorize push, PR creation, or merge.

## Recovery and operator actions

- Use the durable state and safe next action: `scheduler cursor-retry` for an
  eligible terminated Cursor timeout, `scheduler cursor-retry --force` for an
  eligible failed standalone or current sequence-leaf Cursor implementation or
  correction turn, `scheduler review retry` for eligible review failures/capacity
  waits, and `scheduler extend` for an explicitly increased review ceiling after
  `max_iterations_reached`. `--force` on a sequence leaf reuses the same sequence
  and ordinal; a stale leaf with no existing relation is rejected.
- Same-reviewer recovery preserves the authenticated reviewer binding. An
  eligible blocked run may have a successor; the blocked source is immutable.
  `scheduler extend` is a separate explicit same-run transition and does not
  rewrite the submitted configuration.
- Retired-state deletion is only the explicit `scheduler cutover cleanup`
  operator flow. Do not run cleanup, live agents, timer changes, or integration
  installation as a side effect of documentation or automated tests.

## Review standard

During phase/subphase implementation and review, run the tests for the approved
contracts and the regressions affected by the changed production paths. The
repository-wide test suite belongs to the pipeline/integration gate, not every
implementation turn or review round. After a correction, rerun the failed and
affected tests; broaden the selection only to resolve a concrete remaining risk.
Keep mandatory contract coverage intact. Report focused results and the separate
pending pipeline gate honestly; neither implies repository-wide validation.

Reviewers accept or reject staged changes based on the approved phase contract,
normal orchestrator flow, artifact and state integrity, prohibited side effects,
and user-visible safety. Do not block acceptance on exhaustive handling of rare
Git edge cases unless the plan explicitly promises that behavior or the case
breaks the supported normal workflow.

## Scheduler timer operations

For real workstation setup, enablement, recovery, or removal of the packaged
user-systemd scheduler timer in WSL, follow
`docs/operacion/timer-systemd-wsl.md`. Treat timer and WSL lingering changes as
explicit operator actions; do not perform them during implementation or tests
unless the active phase and user explicitly authorize real workstation work.
