# ai_dev_loop

Deterministic local orchestrator for Codex/Cursor development loops.

Phase 7 implements global Codex integrations: the handoff skill, SessionStart hook, and `integrations install` / `uninstall` / `status`. Phase 6's real `abort`, Phase 5's bounded stage-review-fix loop, and `resume` remain unchanged.

## What Phase 7 Implements

- Real `ai_dev_loop integrations install` and `integrations uninstall`
- Global handoff skill at `~/.agents/skills/ai-dev-loop-handoff/SKILL.md`
- SessionStart hook at `~/.codex/hooks/ai_dev_loop_session_start.py`
- Safe idempotent merge into `~/.codex/hooks.json`
- `integrations status` with package-content match reporting
- `doctor` checks for installed skill, hook script, hook registration, and trust guidance
- Minimal Codex session metadata under XDG state; no transcript reads or auth storage

## What Phase 6 Implements

- Real `ai_dev_loop abort <run-id>` with process-group signaling for active Cursor/Codex children
- Durable abort-request marker at `locks/abort-request.json`
- Durable active-child metadata at `locks/active-process.json` while Cursor/Codex streaming subprocesses run
- Workflow abort observation before and after each loop action in `start` and `resume`
- User-aborted child processes end the run as `aborted`, not as generic failure or timeout

## What Phase 5 Implements

- Shared workflow engine used by both `start` and `resume`
- Bounded review/fix loop driven only by schema-validated Codex JSON
- One Cursor chat per run, reused for every implementation and correction turn
- One Codex session per run, resumed for every review
- Exact forwarding of Codex-authored `cursor_fix_prompt` values from `prompts/fixes/NN.txt`
- Real `resume <run-id>` with conservative checkpoint planning

## Recommended Setup (WSL)

Use [uv](https://docs.astral.sh/uv/) so Python 3.11+ is available without modifying system Python:

```bash
uv python install 3.11
uv venv --python 3.11 .venv
source .venv/bin/activate
uv sync --all-extras
```

Or run the helper script:

```bash
bash scripts/development-install.sh
```

### Editable development install

```bash
uv sync --all-extras
uv run ai_dev_loop --help
```

### User-level product install

```bash
uv tool install .
ai_dev_loop --help
```

Optional alternative: `pipx install .` if you already manage CLIs with pipx.

## Global Codex Integrations

Install the user-level Codex assets once per machine:

```bash
ai_dev_loop integrations install
```

This installs:

- `~/.agents/skills/ai-dev-loop-handoff/SKILL.md`
- `~/.codex/hooks/ai_dev_loop_session_start.py`
- a merged SessionStart registration in `~/.codex/hooks.json`

After install:

1. Open `/hooks` in Codex.
2. Trust the `ai_dev_loop` hook. The hook may be skipped until it is trusted.
3. Restart or resume Codex so SessionStart context includes the exact current session ID.

Check installation:

```bash
ai_dev_loop integrations status
ai_dev_loop integrations status --output json
ai_dev_loop doctor
```

The SessionStart hook exposes the exact current Codex session ID through `hookSpecificOutput.additionalContext` and stores only minimal session metadata under:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

The hook never reads transcript contents and never stores auth material.

### Uninstall

```bash
ai_dev_loop integrations uninstall
```

Uninstall removes only `ai_dev_loop` assets. It preserves unrelated hooks, unrelated skills, run history, prompts, reviews, staged patches, and XDG state by default.

## Target Repository Configuration

Create `ai_dev_loop.yaml` at the repository root:

```yaml
version: 1

project:
  name: my-project

cursor:
  command: agent
  model: composer-2.5-fast
  output_format: stream-json
  force: true
  trust_workspace: true
  sandbox: disabled

codex:
  command: codex
  review_model: o4-mini
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write

workflow:
  max_review_iterations: 3
  require_clean_worktree: true
  stage_mode: all
  cursor_timeout_minutes: 90
  codex_timeout_minutes: 90

prompt:
  directory: docs/plans
  filename_template: prompt_{plan_stem}.txt
```

Validate it:

```bash
ai_dev_loop config validate --repo /path/to/repo
```

## Handoff Workflow

From the active Codex session after a plan and Cursor prompt are finalized:

1. Use the `ai-dev-loop-handoff` skill, or follow the same steps manually.
2. Ensure the plan is final and the separate Cursor prompt file exists.
3. Read the repository `ai_dev_loop.yaml`.
4. Use the exact session ID from SessionStart context. Do not infer another session and do not use `--last`.
5. Run `ai_dev_loop prepare` with the exact Cursor prompt on stdin.
6. Exit Codex with `/exit`.
7. Run the returned `ai_dev_loop start <run-id>` command in the shell.

Example prepare:

```bash
ai_dev_loop prepare \
  --repo-path /path/to/repo \
  --plan-path docs/plans/my-plan.md \
  --prompt-source-path docs/plans/prompt_my-plan.txt \
  --codex-session-id "<exact-session-id>" \
  --output json < docs/plans/prompt_my-plan.txt
```

JSON output includes:

```json
{
  "schema_version": 1,
  "status": "prepared",
  "run_id": "my-project-20260704T134512Z-8f43c1",
  "project": "my-project",
  "start_command": "ai_dev_loop start my-project-20260704T134512Z-8f43c1",
  "requires_codex_exit": true
}
```

**Important:** never run `ai_dev_loop start` or `ai_dev_loop resume` from the active Codex TUI.

If SessionStart context is missing, run `ai_dev_loop integrations status`, trust the hook in `/hooks`, restart or resume Codex, or pass `--codex-session-id` only when you have the exact session ID from a trusted source.

## Start A Prepared Run

```bash
ai_dev_loop start <run-id>
```

`start` runs the full bounded loop when possible. Successful completion leaves repository changes staged. If Codex still reports actionable findings when `max_review_iterations` is reached, the run stops in `max_iterations_reached` with the latest review and fix prompt preserved.

## Resume A Checkpointed Run

```bash
ai_dev_loop resume <run-id>
```

`resume` continues from clear checkpoints without rerunning completed agent turns when durable artifacts prove they finished. Supported checkpoints include `prepared`, `waiting_for_cursor_fix`, `staging`, `reviewing`, and `interrupted`.

## Using Abort

Request cancellation while a run is active or waiting at a non-terminal checkpoint:

```bash
ai_dev_loop abort <run-id>
```

Abort preserves repository contents, staged changes, and existing run artifacts. It does not commit, push, reset, clean, stash, unstage, or delete artifacts.

## Inspect Results

```bash
ai_dev_loop status <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop logs <run-id>
ai_dev_loop logs <run-id> --component codex
```

## State Storage

Run artifacts are stored under XDG state, not inside the target repository:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project-slug>/<run-id>/
├── state.json
├── effective-config.yaml
├── source-config.yaml
├── manifest.json
├── plan/
├── prompts/
│   └── fixes/
├── cursor/
│   ├── chat.json
│   └── iterations/
├── codex/
│   ├── events/
│   └── reviews/
├── git/
│   ├── status/
│   └── diffs/
└── logs/
    ├── ai_dev_loop.log
    └── events.jsonl
```

Repository locks live under:

```text
$XDG_STATE_HOME/ai_dev_loop/repository-locks/
```

Fallback when XDG variables are unset:

- config: `~/.config/ai_dev_loop`
- state: `~/.local/state/ai_dev_loop`
- cache: `~/.cache/ai_dev_loop`

## Useful Commands

```bash
ai_dev_loop doctor
ai_dev_loop integrations install
ai_dev_loop integrations status
ai_dev_loop integrations uninstall
ai_dev_loop list
ai_dev_loop status <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop logs <run-id>
ai_dev_loop resume <run-id>
ai_dev_loop abort <run-id>
```

## Development Validation

```bash
uv run python -m pytest -q -s
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m mypy src
uv run python -m build
uv run ai_dev_loop --help
uv run ai_dev_loop integrations status
uv run ai_dev_loop doctor
```

## Safety Model

- `prepare` rejects empty stdin and unrelated dirty worktrees when `require_clean_worktree: true`
- `start` and `resume` re-validate branch, HEAD, plan/prompt hashes, and prepared baseline where applicable
- Correction turns require the staged index to still match the previous orchestrator-recorded staged patch
- Codex review resumes the exact prepared session ID and decides outcomes from structured JSON, not Markdown parsing
- The orchestrator forwards Codex fix prompts exactly as stored; it does not rewrite them
- Repository file inputs must resolve inside the repository root; symlink escapes are rejected
- Subprocesses use direct argument arrays (`shell=False` is never used)
- Prompts, session IDs, staged patches, review artifacts, and agent output files are written with user-only permissions
- The orchestrator stages changes with `git add -A` for `stage_mode: all` but does not commit, push, tag, reset, clean, stash, or unstage
- Global integration install preserves unrelated hooks and does not bypass Codex hook trust

## Privacy And Cleanup

- Default CLI output does not print full prompts, fix prompts, staged patches, review Markdown, raw JSONL, or auth payloads
- Run artifacts remain under XDG state for audit and recovery
- `integrations uninstall` does not delete run history, prompts, reviews, or logs
- The SessionStart hook stores only minimal session metadata and never reads transcript contents

## License

MIT
