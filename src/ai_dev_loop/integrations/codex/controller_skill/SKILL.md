---
name: ai-dev-loop-controller
description: Control an ai_dev_loop run from the original Codex planning session after controller A prepares a run with frozen review model and reasoning effort. Use when the user asks how a run is going, wants to launch a prepared run, abort it, or inspect the next safe action from the controller conversation.
---

# ai_dev_loop Controller (session A)

Use this skill only from the **controller** Codex session (A) after A prepared a
run with `--controller-session-id`, `--codex-review-model`, and
`--codex-review-reasoning-effort`. Reviewer B is created by the worker at the
first review boundary, not by A.

## Identity Rules

1. Use the **exact** current SessionStart session ID as the controller ID.
2. Never infer, shorten, rewrite, search for, or guess session IDs.
3. Never use `--last`.
4. Never resume, write to, or send prompts to reviewer session B from here.
5. Do not pass `--codex-session-id` when preparing a new controller run.
6. For PR-review v2 or any run with a stored reviewer session ID, if the current
   session ID equals that reviewer session ID, **refuse** controller actions and
   explain that B must remain inactive.

## Allowed Commands

Call only these orchestrator commands (argument arrays / CLI; no shell
interpolation of secrets beyond the exact IDs already known):

- `ai_dev_loop controller status`
- `ai_dev_loop launch`
- `ai_dev_loop abort`
- `ai_dev_loop pr-review create|prepare|start|status|history|resume|abort`
  (only when `pr_review_v2.enabled` is true in the target repository config)
- `ai_dev_loop github doctor`
- existing read-only commands: `status`, `logs`, `inspect`, `list`

Do **not** run `prepare` with a reviewer session ID for the current controller
workflow. Do **not** invent notification delivery.

`pr-review create` and `pr-review prepare` only freeze v2 `PreparedState`; they
do not launch workers, agents, GitHub writes, commits, or pushes. Before
`pr-review start`, restate that it is the sole external-effects gate. The v2
worker may commit only the accepted staged patch, non-force push the prepared
branch, create or update the PR, post the idempotent review trigger, reply only
where verified and resolve only verified fixed threads. It never merges,
force-pushes, retargets, uses `--last`, invents a Codex session, or replaces a
stored Cursor chat.

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

### “crea/prepara el PR review” / “create or prepare the PR review”

Only after the local run is `completed` or `completed_with_residual_risk` and
GitHub is enabled:

```bash
ai_dev_loop github doctor --repo-path /path/to/repo --output json
ai_dev_loop pr-review create <source-run-id>
```

`create` is read-only and returns a prepared v2 run. The controller starts it
explicitly only after the user asks to open the write gate.

### “adopta un PR abierto” / independent PR-review prepare + start

When the PR already exists and did not originate from an `ai_dev_loop` source
run, reviewer B prepares (non-mutating) and controller A starts:

From B (inactive afterward):

```bash
ai_dev_loop pr-review prepare \
  --repo OWNER/REPO \
  --pr <number> \
  --codex-session-id "<exact-reviewer-B>" \
  --plan <plan-rel-al-repo> \
  --prompt <prompt-rel-al-repo> \
  --repo-path /path/to/repo \
  [--cursor-chat-id <existing-chat-id>] \
  [--review-model <model>]
```

From A only:

```bash
ai_dev_loop pr-review start <run-id>
```

Never invoke `pr-review start` from reviewer B. The reviewer must remain
inactive after its read-only prepare succeeds.

### “¿cómo va el PR review?” / PR-review status

```bash
ai_dev_loop pr-review status <pr-review-run-id> --output json
```

Report run id, durable state, PR binding, cycle counts, supervisor/lease
liveness, safe error/result, and next action only. Never print GitHub comment
bodies, prompts, patches, tokens, or full identifiers.

### “reanuda el ciclo PR” after interruption

```bash
ai_dev_loop pr-review resume <pr-review-run-id>
```

## Missing Capability Or Ambiguity

- If SessionStart context is missing, stop and ask for remediation via
  `integrations status` / `/hooks` trust / session bridge status.
- If zero or multiple controller matches exist, report the safe next action from
  `controller status` and do not guess.
- If no controller-prepared run exists, tell the user to finish handoff with
  `ai-dev-loop-handoff` first.
