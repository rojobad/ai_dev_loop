---
name: ai-dev-loop-handoff
description: Prepare an approved local Codex implementation plan for the external ai_dev_loop orchestrator from controller session A. Use after a plan and its Cursor prompt are finalized, when the user wants automated Cursor implementation with a fresh read-only Codex reviewer created at the first review boundary.
---

# ai_dev_loop Handoff (controller A)

Use this skill from the active Codex **controller** session (A) after plan and
Cursor prompt approval. A prepares the durable run directly with frozen review
model and reasoning effort. Reviewer B does **not** exist at prepare time; the
worker creates one fresh, read-only Codex CLI reviewer at the first review and
resumes only that identity afterward.

## Identity Boundary

- **A (controller):** the original planning conversation. It prepares, launches,
  monitors, and aborts the run.
- **B (reviewer):** created once at the first review boundary by the worker.
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
`ai_dev_loop prepare` from session A:

- pass the exact approved Cursor prompt on stdin;
- pass `--plan-path` and `--prompt-source-path`;
- pass `--controller-session-id` with A's exact SessionStart session ID;
- pass `--codex-review-model` and `--codex-review-reasoning-effort`;
- do **not** pass `--codex-session-id`;
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
  --controller-session-id "<exact-controller-session-id-A>" \
  --codex-review-model "<exact-review-model>" \
  --codex-review-reasoning-effort "<exact-reasoning-effort>" \
  --output json < /path/to/repo/docs/plans/prompt_my-plan.txt
```

Parse the prepare JSON. For a valid controller prepare:

- `requires_codex_exit` is `false`;
- `launch_command` is present;
- `run_id` identifies the prepared run;
- no reviewer session is bound yet.

Then use the `ai-dev-loop-controller` skill to launch and monitor from A.

## PR-review v2 prepare from B

When `pr_review_v2.enabled` is true and the user wants to adopt an
**already-open PR** (not created from a completed `ai_dev_loop` source run),
reviewer session B runs the read-only v2 prepare command:

```bash
ai_dev_loop pr-review prepare \
  --repo OWNER/REPO \
  --pr <number> \
  --codex-session-id "<exact-reviewer-B>" \
  --plan <plan-rel-al-repo> \
  --prompt <prompt-rel-al-repo> \
  [--repo-path /path/to/repo]
```

Return the prepared run id to A through the authorized app message path only.
Controller session A alone opens the external-effects gate with
`ai_dev_loop pr-review start <run-id>`; do not use retired v1 flags,
`set-cursor-model`, `continue`, or `recover`. Never invoke `pr-review start`
from reviewer B.

## Legacy Single-Session Note

If the user explicitly requests the older local workflow without a controller,
omit `--controller-session-id`, pass `--codex-session-id`, and tell the user to
exit the Codex TUI before `ai_dev_loop start`. That direct legacy path remains
unchanged until the documented cutover.

## Recovery When Session Context Is Missing

If SessionStart context does not include the current session ID:

1. ask the user to run `ai_dev_loop integrations status --target codex-desktop-wsl`
   when using Codex Desktop on Windows with WSL agents;
2. for Codex Desktop on Windows, tell them to open `/hooks` in Codex Desktop and
   trust the `ai_dev_loop` hook;
3. only if the user can supply the exact session ID from a trusted source, pass
   it manually for legacy flows that still require it;
4. do not guess, shorten, or substitute another session ID;
5. do not use `--last`.
