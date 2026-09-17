---
name: ai-dev-loop-fallback-recovery
description: Recover one blocked or abandoned ai_dev_loop implementation by reproducing its complete staged patch in a new isolated worktree and submitting one fresh ordinary run. Use when native reviewer retry is unavailable or unsafe and the source worktree has a stable, fully staged implementation. Do not use for ordinary retries, active runs, mixed staged/unstaged work, or recursive recovery attempts.
---

# ai_dev_loop Fallback Recovery

Use this skill for the agent-guided fallback based on an isolated Git worktree
and a recovery-specific Cursor prompt. This is deliberately not a scheduler
recovery feature. Do not invoke `scheduler recovery ...`.

The objective is one fresh ordinary run whose first Cursor turn mechanically
reproduces the already-staged implementation. The plan remains unchanged, a
fresh reviewer evaluates the result, and the source worktree remains untouched.

## Authorization boundaries

- Read-only diagnosis is allowed when the user asks how to recover.
- Creating a branch/worktree and submitting or starting a replacement run
  requires an explicit user request for this fallback.
- Commit, integration, push, worktree removal, branch deletion, abort, and
  installation require their own explicit authority unless the user already
  included that action in the request.
- Never reset, clean, unstage, stash, commit, or otherwise repair the source
  worktree.

## Required companion skills

Before acting, read and follow the current installed versions of:

- `ai-dev-loop-handoff` for normal scheduler submission;
- `ai-dev-loop-controller` for the single start and read-only monitoring.

Use `create-cursor-plan` only if the original plan or prompt is missing or the
user wants to change scope. A changed plan is a new implementation task, not
this fallback.

## Admission decision

Proceed only when all of these facts are established:

1. The exact source repository and run are known.
2. The source run is terminal, safely aborted, or otherwise has no active owned
   process. Do not race an active Cursor or reviewer.
3. The source repository has one nonempty staged implementation, no tracked
   unstaged changes, and no untracked non-ignored files.
4. Its branch, HEAD, repository identity, staged binary-patch SHA-256, changed
   paths, and diff statistics have been captured read-only.
5. The approved plan is tracked at that HEAD and unchanged in the source
   worktree. The approved prompt exists and still describes that plan.
6. The staged scope is credible for the plan. If it is unexpectedly broad,
   generated, duplicated, or already contains unrelated work, stop and report
   it instead of laundering it through a fresh review.
7. This is the first fallback generation for that implementation. Never chain
   another fallback automatically after this one fails.

If any requirement is false or cannot be proven, stop. Ask for a new plan or
manual source cleanup; do not infer which files belong to the implementation.

## Workflow

Read [references/workflow.md](references/workflow.md) before creating the
worktree. Follow it as a safety checklist, adapting names and paths to the exact
repository rather than copying placeholders literally.

At a high level:

1. Inspect and fingerprint the source without mutation.
2. Create a sibling worktree on a new dedicated branch at the exact source
   HEAD. Never reuse an existing path or branch ambiguously.
3. Reuse the approved plan byte-for-byte. Create an ignored prompt source in
   the new worktree by preserving the approved prompt and appending only the
   recovery envelope from the reference.
4. The envelope must bind the absolute source path, source HEAD, staged patch
   SHA-256, and read-only rule. It must tell Cursor to reproduce the existing
   patch mechanically, not redesign or expand it.
5. Verify the fallback worktree is clean apart from the intentionally ignored
   prompt. Submit it through `ai-dev-loop-handoff` with explicit reviewer model
   and reasoning effort, then start it exactly once through
   `ai-dev-loop-controller`.
6. Monitor only through safe controller status. Never contact or resume the
   reviewer session and never call start twice.
7. Treat `completed` and `completed_with_residual_risk` as review outcomes, not
   authorization to commit or integrate. At terminal success, compare paths and
   diff statistics with the source and report any scope growth before asking
   for the next action.

## Stop conditions

Stop the fallback and report instead of improvising when:

- source state or staged hash changes after fingerprinting;
- worktree/branch creation is ambiguous or conflicts with an existing target;
- the plan is modified, absent from HEAD, or disagrees with the staged patch;
- Cursor cannot reproduce the source patch hash on the identical base;
- the new run blocks, aborts, reaches its review ceiling, or ends with P1
  findings;
- correction rounds materially broaden the changed paths or implementation
  size beyond the original plan;
- the user asks for a second fallback generation.

One failed bounded fallback is evidence that the implementation needs a new
plan or manual decomposition. Do not recursively create another worktree/run,
raise the review budget, or rewrite the prompt to chase acceptance.

## Handoff result

Report concisely:

- source HEAD and abbreviated staged-patch fingerprint;
- fallback worktree and branch;
- prepared run identity and safe state;
- original versus terminal changed-path/diff statistics;
- whether the source remained byte-identical;
- the next action requiring user authorization.

Do not print prompts, patches, raw agent output, full reviewer/session IDs, or
sensitive environment data.
