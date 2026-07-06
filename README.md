# ai_dev_loop

Deterministic local orchestrator for Codex/Cursor development loops.

Phase 4 adds Codex review after Git staging. A real `ai_dev_loop start <run-id>` now runs start preflight, acquires run/repository locks, probes local Git/Cursor/Codex CLIs, creates or reuses one Cursor chat, executes the prepared prompt headlessly, stages repository changes with `git add -A` when `stage_mode: all`, resumes the exact prepared Codex session for review, validates the schema-constrained review result, and stops with either a completed outcome or a `waiting_for_cursor_fix` boundary when findings exist.

The full automated loop is **still incomplete**: Cursor correction turns, `resume`, and `abort` are not implemented yet.

## What Phase 4 Implements

- Structured orchestrator event logging at `logs/events.jsonl`
- Codex review via `codex exec --cd <repo> --sandbox <sandbox> resume ... <exact-session-id> -`
- Deterministic review wrapper prompt that invokes the configured target-repository review skill (for example `$review-staged-cursor-execution`)
- Schema-validated `codex-review-result-v1` handling with cross-field validation
- Codex review artifacts:
  - `codex/events/01.jsonl` and `codex/events/01.stderr.txt`
  - `codex/reviews/01.json`, `codex/reviews/01.md`, and `codex/reviews/01.metadata.json`
  - `prompts/fixes/01.txt` when actionable findings exist
- Durable `state.iterations` metadata with real `codex` and `review` sections
- Terminal outcomes:
  - `completed` when there are no actionable findings and tests passed or were not applicable
  - `completed_with_residual_risk` when tests failed, were blocked, or reported residual risk without actionable findings
  - `waiting_for_cursor_fix` when Codex returns actionable findings and stores the exact correction prompt

## What Phase 3 Still Provides

- Git staging after a successful Cursor implementation turn
- Post-Cursor safety checks before staging
- `git add -A` for `stage_mode: all` only
- Staged diff artifacts under `git/status/` and `git/diffs/`
- A successful real `start` may leave changes **staged** in the target repository

## What Phase 2 Still Provides

- Real `ai_dev_loop start <run-id>` preflight, locks, CLI probes, and Cursor execution
- Cursor chat creation via `agent create-chat` with immediate persistence
- Cursor headless execution with process-group timeout handling and durable artifacts:
  - `cursor/iterations/01/events.jsonl`
  - `cursor/iterations/01/stderr.txt`
  - `cursor/iterations/01/final.txt` (when detected)
  - `cursor/iterations/01/metadata.json`
  - `git/status/01-before-cursor.txt` and `git/status/01-after-cursor.txt`

## What Phase 1 Still Provides

- Installable Python 3.11+ package with console entry point `ai_dev_loop`
- Real `ai_dev_loop prepare` that reads the exact Cursor prompt from stdin
- `ai_dev_loop config validate`
- Read-only inspection: `status`, `list`, `inspect`, `logs`
- Non-mutating `doctor` and `integrations status`
- XDG config/state/cache paths outside target repositories
- Secure permissions (`0700` directories, `0600` sensitive files)
- Typed configuration and run-state models with JSON schemas
- Atomic writes and SHA-256 artifact hashes
- Git discovery and clean-worktree safety checks

## What Is Still Pending

- Cursor correction turns from stored Codex fix prompts
- The complete bounded stage-review-fix loop
- `resume` recovery semantics and `abort` child-process termination
- Global Codex skill and SessionStart hook installation
- `integrations install` / `uninstall`

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

## Prepare A Run

From the active Codex session, pipe the exact approved Cursor prompt on stdin:

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

**Important:** exit the active Codex TUI before running `start`.

## Start A Prepared Run

```bash
ai_dev_loop start <run-id>
```

On success with no actionable findings, the run moves to `completed` and the CLI reports that changes remain staged. When Codex returns findings, the run stops in `waiting_for_cursor_fix` with a stored correction prompt; correction execution is not implemented yet.

Inspect results:

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
ai_dev_loop integrations status
ai_dev_loop list
ai_dev_loop status <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop logs <run-id>
```

## Development Validation

```bash
uv run python -m pytest -q -s
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m mypy src
uv run python -m build
uv run ai_dev_loop --help
```

## Safety Model

- `prepare` rejects empty stdin and unrelated dirty worktrees when `require_clean_worktree: true`
- `start` re-validates branch, HEAD, plan/prompt hashes, and worktree baseline before invoking Cursor
- After Cursor, `start` re-validates plan hash and prompt-source safety before `git add -A`
- Codex review resumes the exact prepared session ID and decides outcomes from structured JSON, not Markdown parsing
- Repository file inputs must resolve inside the repository root; symlink escapes are rejected
- Subprocesses use direct argument arrays (`shell=False` is never used)
- Prompts, session IDs, staged patches, review artifacts, and agent output files are written with user-only permissions
- The orchestrator stages changes with `git add -A` for `stage_mode: all` but does not commit, push, tag, reset, clean, stash, or unstage

## License

MIT
