---
name: ai-dev-loop-handoff
description: Submit an approved local Codex implementation plan to the central ai_dev_loop scheduler from controller session A. Use after a plan and its Cursor prompt are finalized, when the user wants automated Cursor implementation with a fresh read-only Codex reviewer created at the first review boundary.
---

# ai_dev_loop Handoff (controller A)

Use this skill from the active Codex **controller** session (A) after plan and
Cursor prompt approval. A submits the durable scheduler run with frozen review
model and reasoning effort. Reviewer B does **not** exist at submit time; the
scheduler creates one fresh, read-only Codex CLI reviewer at the first review and
resumes only that identity afterward.

## Identity Boundary

- **A (controller):** the original planning conversation. It submits, starts,
  monitors, and aborts the run through the scheduler.
- **B (reviewer):** created once at the first review boundary by the scheduler.
  A must not fork, message, or supply a pre-existing B session ID.

Do not infer, shorten, rewrite, search for, or guess session IDs. Never use
`--last`. Never create a Codex session from a shell workaround.

## Before You Start

1. Confirm the implementation plan is final and approved.
2. Confirm the separate Cursor prompt file exists and matches the approved plan.
3. Read the target repository's `ai_dev_loop.yaml`.
4. Choose the exact `--codex-review-model` and `--codex-review-reasoning-effort`
   values A will freeze into the run. They must be explicit CLI parameters; do
   not rely on YAML or session defaults.

## Controller Workflow

From the target repository root (or with explicit `--repo-path`), run
`ai_dev_loop scheduler submit` from session A:

- pass the exact approved Cursor prompt on stdin;
- pass `--plan-path` and `--prompt-source-path`;
- pass `--controller-session-id` with A's exact SessionStart session ID;
- pass `--codex-review-model` and `--codex-review-reasoning-effort`;
- do **not** pass `--codex-session-id`;
- prefer `--output json`.

When running under Codex Desktop on Windows with WSL agents, Desktop may
propagate `CODEX_HOME` as a Windows/DrvFS path under `/mnt/c/...`. Do **not**
keep that value for submit:

```bash
case "${CODEX_HOME:-}" in
  /mnt/*) unset CODEX_HOME ;;
esac
```

Example shape:

```bash
case "${CODEX_HOME:-}" in
  /mnt/*) unset CODEX_HOME ;;
esac
ai_dev_loop scheduler submit \
  --repo-path /path/to/repo \
  --plan-path docs/plans/my-plan.md \
  --prompt-source-path docs/plans/prompt_my-plan.txt \
  --controller-session-id "<exact-controller-session-id-A>" \
  --codex-review-model "<exact-review-model>" \
  --codex-review-reasoning-effort "<exact-reasoning-effort>" \
  --output json < /path/to/repo/docs/plans/prompt_my-plan.txt
```

Parse the submit JSON. For a valid controller submit:

- `status` is `queued`;
- `run_id` identifies the submitted run;
- no reviewer session is bound yet.

Then use the `ai-dev-loop-controller` skill to start and monitor from A.

## Recovery When Session Context Is Missing

If SessionStart context does not include the current session ID:

1. ask the user to run `ai_dev_loop integrations status --target codex-desktop-wsl`
   when using Codex Desktop on Windows with WSL agents;
2. for Codex Desktop on Windows, tell them to open `/hooks` in Codex Desktop and
   trust the `ai_dev_loop` hook;
3. do not guess, shorten, or substitute another session ID;
4. do not use `--last`.
