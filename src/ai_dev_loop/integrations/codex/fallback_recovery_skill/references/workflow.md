# Bounded worktree fallback workflow

Use this reference only after `SKILL.md` admission succeeds. Every command is a
shape to adapt to already-validated explicit paths; never paste placeholders or
use broad/globbed targets.

## 1. Capture the source checkpoint

From the source repository, collect without mutation:

```bash
git rev-parse --show-toplevel
git rev-parse --git-common-dir
git rev-parse HEAD
git branch --show-current
git status --porcelain=v1 --untracked-files=all
git diff --cached --binary | sha256sum
git diff --cached --stat
git diff --cached --name-only
git diff --binary
```

The cached diff must be nonempty. The ordinary diff must be empty. Reject any
untracked non-ignored file. Record the binary-patch hash as the recovery source
fingerprint.

Verify the plan is tracked at HEAD and the worktree copy is identical:

```bash
git ls-files --error-unmatch <plan-path>
git diff --quiet HEAD -- <plan-path>
```

Inspect scheduler/controller status to prove no owned attempt is active. If the
run is active, use normal controller handling and ask before aborting.

## 2. Choose an isolated destination

Use a descriptive sibling path and dedicated branch. Before mutation, verify
both are absent and list existing worktrees. Refuse collisions rather than
deleting or reusing them.

Create the worktree from the exact captured HEAD:

```bash
git worktree add -b <dedicated-branch> <absolute-fallback-path> <exact-head>
```

Immediately verify its HEAD, branch, and clean status. Do not copy the source
index, `.git` files, scheduler artifacts, SQLite state, or agent session data.

## 3. Build the recovery prompt

Keep the approved plan unchanged. Preserve the approved Cursor prompt and add a
short execution envelope with this meaning:

```text
Use the implementation currently staged in <absolute-source-repository> as
read-only recovery material. The source HEAD is <exact-head> and the expected
SHA-256 of `git diff --cached --binary` is <exact-sha256>.

Before using it, verify both values. Reproduce the complete staged patch in this
fallback worktree mechanically and validate it against the unchanged approved
plan. Do not redesign, refactor, broaden, or improve unrelated code. Do not
modify, stage, commit, reset, clean, stash, or otherwise mutate the source
worktree. If the source values differ, the patch cannot be reproduced on this
base, or the plan conflicts with it, stop and explain the mismatch.
```

Retain the original prompt's requirement to leave implementation changes
unstaged and uncommitted for independent review. The prompt source should use
the repository's normal ignored prompt location so its presence does not dirty
the fallback baseline.

Do not edit the plan to describe the recovery. Recovery mechanics are execution
context, not product scope.

## 4. Submit and start one ordinary run

Read `ai_dev_loop.yaml`. Confirm its review ceiling is bounded and acceptable
to the user; do not edit it merely for the fallback.

Follow `ai-dev-loop-handoff` with the fallback repository, unchanged plan, new
prompt source, and explicit reviewer model/reasoning. Parse and retain the
submitted run identity. Then follow `ai-dev-loop-controller` to start that exact
run once.

Do not use:

- `scheduler recovery`;
- `scheduler review retry` for the abandoned source;
- a pre-existing reviewer session;
- `--last`;
- a second submit/start when output is uncertain.

Resolve uncertainty through read-only status by exact run identity.

## 5. Monitor with a hard generation boundary

Use controller status only. Optional periodic monitoring may be configured only
when the user requests it. Monitoring must not message the reviewer, inspect
raw prompts/events, mutate either worktree, or perform integration.

If the run fails, blocks, is aborted, reaches its review ceiling, or completes
with unresolved P1 findings, stop. Preserve both worktrees and report the safe
next action. Never launch another fallback automatically.

## 6. Verify terminal scope

At terminal success, and only when no attempt is active, capture the fallback
staged changed paths and diff statistics. Compare them with the source
checkpoint. A different accepted patch can be legitimate after reviewer-driven
corrections, but any material scope or size growth must be disclosed before
commit or integration.

Confirm the source HEAD, status, and staged binary-patch hash remain unchanged.
Do not commit, cherry-pick, replace the source index, remove the worktree, or
delete its branch without explicit authorization.
