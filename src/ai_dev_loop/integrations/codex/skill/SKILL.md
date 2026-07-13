---
name: ai-dev-loop-handoff
description: Prepare an approved local Codex implementation plan for the external ai_dev_loop orchestrator using an isolated reviewer session fork. Use after a plan and its Cursor prompt are finalized, when the user wants automated Cursor implementation with review in a separate inactive Codex session.
---

# ai_dev_loop Handoff (A/B)

Use this skill to hand an approved implementation plan from the active Codex
**controller** session (A) to an isolated **reviewer** session (B) that the
external `ai_dev_loop` orchestrator will resume for every automated review.

## Identity Boundary

- **A (controller):** the original planning conversation. It launches, monitors,
  and aborts the run. It must **not** be the session resumed for review.
- **B (reviewer):** a same-directory Codex fork created after plan approval. It
  runs `prepare`, then must remain **inactive/untouched** while the worker runs.
  Every `codex exec resume` uses B's exact session ID only.

Do not infer, shorten, rewrite, search for, or guess session IDs. Never use
`--last`. Never create a Codex session from a shell workaround.

## Before You Start

1. Confirm the implementation plan is final and approved.
2. Confirm the separate Cursor prompt file exists and matches the approved plan.
3. Read the target repository's `ai_dev_loop.yaml`.
4. Confirm the Codex app exposes an authorized same-directory fork capability
   and an authorized way for B to return the prepared run identity to A.
5. If either capability is unavailable, **stop** with a clear explanation. Do
   not fall back to guessed session IDs or external IPC scraping.

## A/B Workflow

### 1. From controller session A

1. Capture A's exact SessionStart session ID. Do not infer another session.
2. After explicit plan/prompt approval, create a **same-directory** app fork to
   session B using only the Codex app capability available to this task.
3. Tell B (via the authorized app message path) to prepare the run with:
   - reviewer session = B (exact SessionStart ID in B);
   - controller session = A (exact ID from this conversation).

### 2. In reviewer session B only

From the target repository root (or with explicit `--repo-path`), run
`ai_dev_loop prepare`:

- pass the exact approved Cursor prompt on stdin;
- pass `--plan-path` and `--prompt-source-path`;
- pass `--codex-session-id` with B's exact SessionStart session ID;
- pass `--controller-session-id` with A's exact session ID;
- prefer `--output json`.

When running under Codex Desktop on Windows with WSL agents, Desktop may
propagate `CODEX_HOME` as a Windows/DrvFS path under `/mnt/c/...`. Do **not**
keep that value for prepare:

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
ai_dev_loop prepare \
  --repo-path /path/to/repo \
  --plan-path docs/plans/my-plan.md \
  --prompt-source-path docs/plans/prompt_my-plan.txt \
  --codex-session-id "<exact-reviewer-session-id-B>" \
  --controller-session-id "<exact-controller-session-id-A>" \
  --output json < /path/to/repo/docs/plans/prompt_my-plan.txt
```

Parse the prepare JSON. For a valid A/B prepare:

- `requires_codex_exit` is `false`;
- `reviewer_must_remain_inactive` is `true`;
- `launch_command` is present for the controller;
- `run_id` identifies the prepared run.

Return the prepared `run_id`, repository path, and `launch_command` to
controller session A through the authorized app message path only.

### 3. Leave B inactive

After prepare succeeds and A has the run identity:

- **Do not** run `ai_dev_loop start`, `launch`, `resume`, `abort`, or status
  control from B.
- Leave B untouched for the duration of the automated run.
- Tell A to use the `ai-dev-loop-controller` skill to launch and monitor.

## Legacy Single-Session Note

If the user explicitly requests the older local workflow without a controller
fork, omit `--controller-session-id`, set `requires_codex_exit` expectations
according to prepare output, and tell the user to exit the Codex TUI before
`ai_dev_loop start`. Prefer the A/B flow for remote Desktop/mobile control.

## Recovery When Session Context Is Missing

If SessionStart context does not include the current session ID:

1. ask the user to run `ai_dev_loop integrations status --target codex-desktop-wsl`
   when using Codex Desktop on Windows with WSL agents;
2. for Codex Desktop on Windows, tell them to open `/hooks` in Codex Desktop and
   trust the `ai_dev_loop` hook;
3. ask the user to run `ai_dev_loop integrations sessions status` when desktop
   sessions must be visible from WSL;
4. only if the user can supply the exact session ID from a trusted source, pass
   it manually;
5. do not guess, shorten, or substitute another session ID;
6. do not use `--last`.
