---
name: ai-dev-loop-controller
description: Control a central scheduler ai_dev_loop run after submit with frozen review model and reasoning effort. Use when the user asks how a run is going, wants to start a submitted run, abort it, or inspect the next safe action.
---

# ai_dev_loop Controller

Use this skill after a scheduler run is submitted with frozen
`--codex-review-model` and `--codex-review-reasoning-effort`. Reviewer B is
created by the scheduler at the first review boundary, not at submit time.

## Identity Rules

1. Exact reviewer B identity remains a scheduler runtime boundary; never resume,
   write to, or send prompts to reviewer session B from here.
2. Controller session A is optional provenance only. Submit, start, status,
   timeline, and abort do not require it.
3. Never infer, shorten, rewrite, search for, or guess session IDs.
4. Never use `--last`.
5. Do not pass `--codex-session-id` when submitting a new run.
6. If the current session ID equals a stored reviewer session ID for the run,
   **refuse** controller actions and explain that B must remain inactive.

## Allowed Commands

Call only these orchestrator commands (argument arrays / CLI; no shell
interpolation of secrets beyond the exact IDs already known):

- `ai_dev_loop controller status`
- `ai_dev_loop scheduler start`
- `ai_dev_loop scheduler abort`
- read-only scheduler commands: `scheduler status`, `scheduler list`,
  `scheduler history`, `scheduler timeline`

Do **not** invent notification delivery.

## Common User Prompts

### “¿cómo va el run?” / “how is the run going?”

Preferred when the exact run ID is known:

```bash
ai_dev_loop controller status \
  --repo-path /path/to/repo \
  --run-id <run-id> \
  --output json
```

Legacy discovery when only controller A provenance was recorded:

```bash
ai_dev_loop controller status \
  --controller-session-id "<exact-controller-session-id-A>" \
  --repo-path /path/to/repo \
  --output json
```

If multiple matches are returned, ask the user which `run_id` to use and pass
`--run-id`. Never choose the latest run by timestamp.

Summarize safely in Spanish when the user wrote in Spanish. Report only:

- run id / scheduler state;
- review budget counters when present;
- safe last error / result;
- next safe action.

Do **not** print full prompts, fix prompts, staged patches, review Markdown,
raw JSONL, full session IDs, auth payloads, or process environments.

### “inicia el run” / “start the run”

```bash
ai_dev_loop scheduler start <run-id> --output json
```

After start, progress depends on the installed scheduler timer or manual
`ai_dev_loop scheduler tick` invocations while WSL is active.

### “aborta el run” / “abort the run”

```bash
ai_dev_loop scheduler abort <run-id>
```

Abort persists a durable abort request and stops only the exact owned scheduler
attempt unit. It does not reset, unstage, clean, or rewrite Git state.

## Missing Capability Or Ambiguity

- If SessionStart context is missing, stop and ask for remediation via
  `integrations status` / `/hooks` trust / session bridge status.
- If zero or multiple controller matches exist, report the safe next action from
  `controller status` and do not guess.
- If no submitted scheduler run exists, tell the user to finish handoff with
  `ai-dev-loop-handoff` first.
