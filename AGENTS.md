# ai_dev_loop Agent Guidance

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

## Review standard

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
