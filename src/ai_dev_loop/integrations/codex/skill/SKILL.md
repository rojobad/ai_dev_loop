---
name: ai-dev-loop-handoff
description: Prepare an approved local Codex implementation plan for the external ai_dev_loop orchestrator. Use after a plan and its Cursor prompt are finalized, when the user wants the same Codex session to review Cursor's staged implementation automatically.
---

# ai_dev_loop Handoff

Use this skill to hand an approved implementation plan from the active Codex session to the external `ai_dev_loop` orchestrator.

## Before You Start

1. Confirm the implementation plan is final and approved.
2. Confirm the separate Cursor prompt file exists and matches the approved plan.
3. Read the target repository's `ai_dev_loop.yaml`.
4. Use the **exact** current Codex session ID from SessionStart context. Do not infer another session and do not use `--last`.

## Prepare The Run

From the target repository root (or with explicit `--repo-path`), run `ai_dev_loop prepare`:

- pass the exact approved Cursor prompt on stdin;
- pass `--plan-path` for the approved plan inside the repository;
- pass `--prompt-source-path` for the prompt source file inside the repository;
- pass `--codex-session-id` with the exact session ID from SessionStart context;
- prefer `--output json` so the result is easy to parse.

When running under Codex Desktop on Windows with WSL agents, Desktop may propagate
`CODEX_HOME` as a Windows/DrvFS path under `/mnt/c/...`. Do **not** keep that value
for prepare: session runtime lookup and the desktop bridge expect the native WSL
`~/.codex` home. Before invoking prepare, unset an unsafe propagated value, for example:

```bash
case "${CODEX_HOME:-}" in
  /mnt/*) unset CODEX_HOME ;;
esac
```

`ai_dev_loop prepare` itself also falls back to the native WSL Codex home when it
detects a DrvFS `CODEX_HOME`, but sanitizing the environment keeps handoff deterministic.

Example shape:

```bash
case "${CODEX_HOME:-}" in
  /mnt/*) unset CODEX_HOME ;;
esac
ai_dev_loop prepare \
  --repo-path /path/to/repo \
  --plan-path docs/plans/my-plan.md \
  --prompt-source-path docs/plans/prompt_my-plan.txt \
  --codex-session-id "<exact-session-id>" \
  --output json < /path/to/repo/docs/plans/prompt_my-plan.txt
```

Parse the prepare result. The JSON includes `run_id`, `start_command`, and `requires_codex_exit`.

## Handoff To The User

**Never** run `ai_dev_loop start` from the active Codex TUI.

Tell the user to:

1. exit Codex with `/exit`;
2. run the exact `start_command` returned by prepare in their shell, for example `ai_dev_loop start <run-id>`.

The orchestrator will run Cursor implementation, stage changes, resume this same Codex session for review, and continue the bounded review/fix loop according to `ai_dev_loop.yaml`.

## Recovery When Session Context Is Missing

If SessionStart context does not include the current session ID:

1. ask the user to run `ai_dev_loop integrations status --target codex-desktop-wsl` when using Codex Desktop on Windows with WSL agents;
2. for Codex Desktop on Windows, tell them to open `/hooks` in Codex Desktop and trust the `ai_dev_loop` hook (trusting the WSL CLI hook browser is not sufficient);
3. ask the user to run `ai_dev_loop integrations sessions status` and ensure the desktop session bridge is healthy when resuming desktop-originated sessions from WSL;
4. use `ai_dev_loop integrations sessions list` only as a manual recovery path to discover desktop session IDs from rollout filenames;
5. only if the user can supply the exact session ID from a trusted source, pass `--codex-session-id` manually to `ai_dev_loop prepare`;
6. do not guess, shorten, or substitute another session ID;
7. do not use `--last`.
