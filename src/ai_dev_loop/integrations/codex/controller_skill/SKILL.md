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
- existing read-only commands: `status`, `logs`, `inspect`, `list`

Do **not** run `prepare` from the controller for an A/B run that B already
prepared. Do **not** invent notification delivery.

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

## Missing Capability Or Ambiguity

- If SessionStart context is missing, stop and ask for remediation via
  `integrations status` / `/hooks` trust / session bridge status.
- If zero or multiple controller matches exist, report the safe next action from
  `controller status` and do not guess.
- If the fork/message capability was never completed and no A/B run exists, tell
  the user to finish handoff with `ai-dev-loop-handoff` first.
