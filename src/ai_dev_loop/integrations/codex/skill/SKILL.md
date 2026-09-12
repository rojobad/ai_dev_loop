---
name: ai-dev-loop-handoff
description: Submit an approved local Codex implementation plan to the central ai_dev_loop scheduler. Use after a plan and its Cursor prompt are finalized, when the user wants automated Cursor implementation with a fresh read-only Codex reviewer created at the first review boundary.
---

# ai_dev_loop Handoff

Use this skill after plan and Cursor prompt approval. Submit the durable
scheduler run with frozen review model and reasoning effort. Reviewer B does
**not** exist at submit time; the scheduler creates one fresh, read-only Codex CLI
reviewer at the first review and resumes only that identity afterward.

## Identity Boundary

- **Optional controller provenance (A):** an optional Codex session ID recorded
  only as provenance. It is not required to submit, start, monitor, or abort.
- **B (reviewer):** created once at the first review boundary by the scheduler.
  Do not fork, message, or supply a pre-existing B session ID.

Do not infer, shorten, rewrite, search for, or guess session IDs. Never use
`--last`. Never create a Codex session from a shell workaround.

## Before You Start

1. Confirm the implementation plan is final and approved.
2. Confirm the separate Cursor prompt file exists and matches the approved plan.
3. Read the target repository's `ai_dev_loop.yaml`.
4. Choose the exact `--codex-review-model` and `--codex-review-reasoning-effort`
   values to freeze into the run. They must be explicit CLI parameters; do not
   rely on YAML or session defaults.

## Submit Workflow

From the target repository root (or with explicit `--repo-path`), run
`ai_dev_loop scheduler submit`:

- pass the exact approved Cursor prompt on stdin;
- pass `--plan-path` and `--prompt-source-path`;
- pass `--codex-review-model` and `--codex-review-reasoning-effort`;
- optionally pass `--controller-session-id` only when you want to record A as
  provenance;
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
  --codex-review-model "<exact-review-model>" \
  --codex-review-reasoning-effort "<exact-reasoning-effort>" \
  --output json < /path/to/repo/docs/plans/prompt_my-plan.txt
```

Parse the submit JSON. For a valid submit:

- `status` is `queued`;
- `run_id` identifies the submitted run;
- no reviewer session is bound yet.

Then use the `ai-dev-loop-controller` skill to start and monitor the run.

## Recovery When Session Context Is Missing

If SessionStart context does not include the current session ID:

1. ask the user to run `ai_dev_loop integrations status --target codex-desktop-wsl`
   when using Codex Desktop on Windows with WSL agents;
2. for Codex Desktop on Windows, tell them to open `/hooks` in Codex Desktop and
   trust the `ai_dev_loop` hook;
3. do not guess, shorten, or substitute another session ID;
4. do not use `--last`.
