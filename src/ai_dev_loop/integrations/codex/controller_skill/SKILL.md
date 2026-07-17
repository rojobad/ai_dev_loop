---
name: ai-dev-loop-controller
description: Control an ai_dev_loop run from the original Codex planning session after an A/B handoff. Use when the user asks how a run is going, wants to launch a prepared run, abort it, or inspect the next safe action from the controller conversation.
---

# ai_dev_loop Controller (session A)

Use this skill only from the **controller** Codex session (A) after
`ai-dev-loop-handoff` prepared a run with a distinct inactive reviewer session
(B).

## Identity Rules

1. Use the **exact** current SessionStart session ID as the controller ID.
2. Never infer, shorten, rewrite, search for, or guess session IDs.
3. Never use `--last`.
4. Never resume, write to, or send prompts to reviewer session B from here.
5. If the current session ID equals the run's reviewer session ID, **refuse**
   controller actions and explain that B must remain inactive.

## Allowed Commands

Call only these orchestrator commands (argument arrays / CLI; no shell
interpolation of secrets beyond the exact IDs already known):

- `ai_dev_loop controller status`
- `ai_dev_loop launch`
- `ai_dev_loop abort`
- `ai_dev_loop pr-review prepare|start|set-cursor-model|create|status|continue|resume|abort`
  (only when `github.enabled` is true in the target repository config)
- `ai_dev_loop github doctor`
- existing read-only commands: `status`, `logs`, `inspect`, `list`

Do **not** run `prepare` from the controller for an A/B run that B already
prepared. Do **not** invent notification delivery.

Before `pr-review create`, restate the autonomous write scope: the worker may
commit the accepted staged patch, non-force push the prepared branch, create or
update the PR to `master`, post `@codex review`, reply inline, and resolve only
verified fixed threads. It never merges, force-pushes, retargets, or uses
`--last` / a new Codex session / a new Cursor chat.

Before `pr-review start` for an **independent** cycle, restate that prepare was
non-mutating and start is the explicit write gate: one idempotent `@codex review`
marker at the bound PR head, then detached polling. Independent cycles create a
new Cursor chat only after all eligible bot findings are actionable.

## Common User Prompts

### “¿cómo va el run?” / “how is the run going?”

```bash
ai_dev_loop controller status \
  --controller-session-id "<exact-controller-session-id-A>" \
  --repo-path /path/to/repo \
  --output json
```

If multiple matches are returned, ask the user which `run_id` to use and pass
`--run-id`. Never choose the latest run by timestamp.

Summarize safely in Spanish when the user wrote in Spanish. Report only:

- run id / status / iteration;
- whether the detached worker is live;
- safe last error / result;
- next safe action.

Do **not** print full prompts, fix prompts, staged patches, review Markdown,
raw JSONL, full session IDs, auth payloads, or process environments.

### “lanza el run” / “launch the run”

Confirm B must remain inactive, then:

```bash
ai_dev_loop launch <run-id> \
  --controller-session-id "<exact-controller-session-id-A>" \
  --output json
```

If a live worker already owns the run, report that status instead of launching
again.

### “aborta el run” / “abort the run”

```bash
ai_dev_loop abort <run-id>
```

Abort persists a durable abort request and signals only clearly owned active
child process groups. It does not reset, unstage, clean, or rewrite Git state.

### “crea el PR” / “create the PR review cycle”

Only after the local run is `completed` or `completed_with_residual_risk` and
GitHub is enabled:

```bash
ai_dev_loop github doctor --repo-path /path/to/repo --output json
ai_dev_loop pr-review create <source-run-id> --output json
```

### “adopta un PR abierto” / independent PR-review prepare + start

When the PR already exists and did not originate from an `ai_dev_loop` source
run, reviewer B prepares (non-mutating) and controller A starts:

From B (inactive afterward):

```bash
ai_dev_loop pr-review prepare \
  --repo-path /path/to/repo \
  --pr <number> \
  --branch <exact-head-branch> \
  --plan-path <plan.md> \
  --prompt-source-path <prompt.txt> \
  --codex-session-id "<exact-reviewer-B>" \
  --controller-session-id "<exact-controller-A>" \
  --output json < /path/to/exact-cursor-prompt.txt
```

Optional before start (only while no Cursor chat exists):

```bash
ai_dev_loop pr-review set-cursor-model <run-id> --cursor-model <model> --output json
```

From A only:

```bash
ai_dev_loop pr-review start <run-id> \
  --controller-session-id "<exact-controller-A>" \
  --output json
```

Never invoke `pr-review start` from reviewer B. For single-session mode, omit
`--controller-session-id` and ensure the reviewer session is inactive before
start.

### “¿cómo va el PR review?” / PR-review status

```bash
ai_dev_loop pr-review status <pr-review-run-id> --output json
```

Report origin, lifecycle, PR number, cycle counts, Cursor model mutability, and
safe next action only. Never print GitHub comment bodies, fix prompts, or tokens.

### “continúa el PR review” after user attention

Ordinary prose is never authorization. Continue only when GitHub has an exact
`@rojobad /ai-dev-loop continue` comment, then:

```bash
ai_dev_loop pr-review continue <pr-review-run-id>
```

### “reanuda el ciclo PR” after interruption

```bash
ai_dev_loop pr-review resume <pr-review-run-id>
```

## Missing Capability Or Ambiguity

- If SessionStart context is missing, stop and ask for remediation via
  `integrations status` / `/hooks` trust / session bridge status.
- If zero or multiple controller matches exist, report the safe next action from
  `controller status` and do not guess.
- If the fork/message capability was never completed and no A/B run exists, tell
  the user to finish handoff with `ai-dev-loop-handoff` first.
