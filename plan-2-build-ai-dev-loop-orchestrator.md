# Build the `ai_dev_loop` Local Development Orchestrator

## Objective

Build a complete, production-quality local orchestration system named `ai_dev_loop`.

The system must automate the user's full local AI development cycle:

1. A local interactive Codex CLI session owns the architectural discussion and creates an approved implementation plan.
2. The same session prepares a run and hands it off to `ai_dev_loop`.
3. The user exits the interactive Codex TUI.
4. `ai_dev_loop` creates a Cursor CLI chat and sends the approved implementation prompt.
5. Cursor implements the plan in the existing WSL repository and worktree.
6. `ai_dev_loop` stages the resulting changes.
7. `ai_dev_loop` resumes the exact original Codex session and asks it to review staged changes with the configured project review skill.
8. The same Codex session returns:
   - the review report;
   - a machine-readable decision;
   - when findings exist, the exact correction prompt for Cursor.
9. `ai_dev_loop` sends that correction prompt to the exact same Cursor chat.
10. The stage-review-fix cycle repeats until:
    - Codex reports no actionable findings; or
    - the configured maximum number of review iterations is reached.
11. The system leaves the final repository changes staged for the user and records a complete local audit trail.

Implement the complete system now. Do not split the work into future phases or defer the automated loop.

## Core Roles

### Codex

Codex is the architect, analyst, context owner, prompt author, and reviewer.

The original Codex session must be resumed for every review. A new Codex session is not an acceptable substitute.

### Cursor

Cursor is the implementation agent.

The initial implementation and all finding corrections must use the same Cursor chat ID.

### `ai_dev_loop`

`ai_dev_loop` is a deterministic local orchestrator.

It may:

- validate inputs;
- persist state;
- execute local CLIs;
- wait without consuming model tokens;
- capture logs;
- stage changes;
- resume sessions;
- enforce iteration limits;
- transport exact prompts;
- control retries and recovery.

It must not invent architecture, reinterpret findings, or replace either agent's reasoning.

## Operating Environment

The primary supported environment is:

- WSL on Windows;
- target repositories stored and executed inside WSL;
- Cursor editor connected to WSL;
- Cursor CLI installed inside WSL as `agent`;
- Codex CLI installed inside WSL as `codex`;
- no cloud agents;
- no remote implementation workers;
- all repository edits and tests executed locally.

The design may remain portable to native Linux, but WSL is the required acceptance environment.

## Technology and Packaging

Implement `ai_dev_loop` as an installable Python CLI application.

Requirements:

- Python 3.11 or newer.
- Standard `pyproject.toml` packaging.
- Console entry point named exactly:

```text
ai_dev_loop
```

- Typed code.
- A clear separation between:
  - CLI presentation;
  - configuration;
  - state persistence;
  - subprocess runners;
  - Git safety;
  - Codex integration;
  - Cursor integration;
  - installation of global Codex assets.
- Use mature libraries where they materially improve reliability, such as:
  - Typer or Click for CLI;
  - Pydantic for configuration and state models;
  - PyYAML or ruamel.yaml for YAML;
  - platformdirs or explicit XDG path handling.
- Keep dependencies focused and pinned with sensible lower bounds.
- Support installation without modifying the system Python. Prefer `uv` for both development and user-level product installation, including a project-local virtual environment and a `uv`-managed Python 3.11+ runtime when the host only provides an older Python.
- Support a standard user-level install path such as `uv tool install .`. `pipx install .` may be documented as an optional alternative, but must not be the only supported installation path.
- Document editable development installation separately, using a local `.venv` created by `uv` by default.

Do not implement the main workflow as a collection of loosely coupled Bash scripts.

## Naming

Use `ai_dev_loop` consistently for:

- executable name;
- Python package;
- XDG directories;
- documentation;
- environment-variable prefix;
- schemas;
- generated integration names.

Do not use:

- `ai_loop`;
- `.ai_loop`;
- `ai-loop`;
- `ai:loop`.

A skill folder may use a hyphenated slug only when the skill specification requires a filesystem-friendly name, but its frontmatter name must clearly refer to `ai_dev_loop`.

## External Storage and XDG Paths

The target repository must not know where the loop stores state.

Resolve paths using XDG variables with these fallbacks:

```text
Configuration:
$XDG_CONFIG_HOME/ai_dev_loop
fallback: ~/.config/ai_dev_loop

Persistent state:
$XDG_STATE_HOME/ai_dev_loop
fallback: ~/.local/state/ai_dev_loop

Cache:
$XDG_CACHE_HOME/ai_dev_loop
fallback: ~/.cache/ai_dev_loop
```

Create directories with user-only permissions where supported:

- directories: `0700`;
- files containing prompts, session IDs, or agent output: `0600`.

Do not persist API keys or authentication files.

## Repository Layout

Use a structure equivalent to:

```text
ai_dev_loop/
├── pyproject.toml
├── README.md
├── LICENSE
├── src/
│   └── ai_dev_loop/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── paths.py
│       ├── state.py
│       ├── locking.py
│       ├── process.py
│       ├── redaction.py
│       ├── errors.py
│       ├── commands/
│       │   ├── prepare.py
│       │   ├── start.py
│       │   ├── resume.py
│       │   ├── status.py
│       │   ├── list_runs.py
│       │   ├── logs.py
│       │   ├── inspect.py
│       │   ├── abort.py
│       │   ├── doctor.py
│       │   └── integrations.py
│       ├── runners/
│       │   ├── cursor.py
│       │   ├── codex.py
│       │   └── git.py
│       ├── integrations/
│       │   └── codex/
│       │       ├── session_start.py
│       │       ├── hook_template.json
│       │       └── skill/
│       │           └── SKILL.md
│       └── schemas/
│           ├── project-config-v1.json
│           ├── run-state-v1.json
│           └── codex-review-result-v1.json
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
└── scripts/
    └── development-install.sh
```

Adapt names where justified, but preserve these responsibilities.

## CLI Surface

Implement at least these commands:

```text
ai_dev_loop prepare
ai_dev_loop start <run-id>
ai_dev_loop resume <run-id>
ai_dev_loop status <run-id>
ai_dev_loop list
ai_dev_loop logs <run-id>
ai_dev_loop inspect <run-id>
ai_dev_loop abort <run-id>
ai_dev_loop doctor
ai_dev_loop integrations install
ai_dev_loop integrations uninstall
ai_dev_loop integrations status
ai_dev_loop config validate
```

All commands must have:

- useful `--help`;
- predictable exit codes;
- human-readable output by default;
- `--output json` where machine consumption is relevant;
- no shell-evaluated output.

## Project Configuration Contract

Target repositories provide a root file named:

```text
ai_dev_loop.yaml
```

Support this initial schema:

```yaml
version: 1

project:
  name: parish360-platform

cursor:
  command: agent
  model: composer-2.5-fast
  output_format: stream-json
  force: true
  trust_workspace: true
  sandbox: disabled

codex:
  command: codex
  review_model: <valid-local-codex-model-slug>
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

### Configuration Precedence

Use this precedence order:

1. Explicit CLI flags.
2. Repository `ai_dev_loop.yaml`.
3. Optional user-global configuration under the XDG config directory.
4. Safe built-in defaults.

`prepare` must store both:

- the effective resolved configuration;
- a snapshot of the source project configuration.

Explicit values that conflict with the file should be recorded. By default, reject safety-sensitive mismatches rather than silently accepting them.

### Configuration Validation

Validate:

- schema version;
- required fields;
- project slug format;
- executable names;
- non-empty model names;
- supported output formats;
- supported sandbox values;
- positive timeout values;
- positive review limit;
- prompt filename template;
- valid review skill name.

Never parse project YAML with regular expressions.

## Global Codex Integration

The application must install everything required for the current Codex session to identify itself and perform the handoff.

### Global Skill

Install a user-global skill at:

```text
$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md
```

This is the user-level skill discovery location. Do not install it under a deprecated or invented `~/.codex/skills` path.

The skill frontmatter must be equivalent to:

```yaml
---
name: ai-dev-loop-handoff
description: Prepare an approved local Codex implementation plan for the external ai_dev_loop orchestrator. Use after a plan and its Cursor prompt are finalized, when the user wants the same Codex session to review Cursor's staged implementation automatically.
---
```

The skill must instruct Codex to:

- ensure the plan is final;
- ensure the separate prompt file exists;
- read `ai_dev_loop.yaml`;
- use the current session ID from SessionStart context;
- run `ai_dev_loop prepare`;
- pass the exact prompt through stdin;
- parse the prepare result;
- never start the loop from the active TUI;
- tell the user to exit and run the returned command.

The project-specific planning skill may invoke the CLI directly, but the global skill must provide a reusable generic workflow and a recovery path for projects without a customized planning skill.

### SessionStart Hook

Install a user-global Codex `SessionStart` hook.

Codex command hooks receive JSON on stdin containing, among other fields:

- `session_id`;
- `transcript_path`;
- `cwd`;
- `hook_event_name`;
- `model`;
- `source` for SessionStart.

Implement a hook script that:

1. Reads and validates one JSON object from stdin.
2. Accepts SessionStart sources:
   - `startup`;
   - `resume`;
   - `clear`;
   - `compact`.
3. Writes a minimal session record under the XDG state directory, containing:
   - session ID;
   - current model;
   - cwd;
   - transcript path if supplied;
   - source;
   - timestamp.
4. Does not read or copy transcript contents.
5. Returns valid JSON with `hookSpecificOutput.additionalContext`.
6. Adds developer context equivalent to:

```text
ai_dev_loop integration is active.
Current Codex session ID: <session-id>
Current Codex model: <model>
Current session working directory: <cwd>
When preparing an approved ai_dev_loop run, pass this exact session ID. Do not infer another session and do not use --last.
```

7. Exits safely with no destructive side effects if input is incomplete.
8. Never exposes authentication data.

### Hook Registration

Register the hook in:

```text
~/.codex/hooks.json
```

Use a definition equivalent to:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <absolute-installed-hook-path>",
            "statusMessage": "Loading ai_dev_loop session context"
          }
        ]
      }
    ]
  }
}
```

The installer must:

- preserve existing hooks;
- merge idempotently;
- avoid duplicate entries;
- validate JSON before writing;
- back up the original file;
- write atomically;
- support uninstall without deleting unrelated hooks;
- tell the user to open `/hooks` and trust the new or changed hook definition;
- report that the hook may be skipped until trusted.

Do not automatically bypass Codex hook trust in normal use.

## Prepare Command

`prepare` is invoked from the active original Codex session.

It creates a run but does not start Cursor or resume Codex.

### Required Inputs

Support explicit flags equivalent to:

```text
--config-path
--project-name
--repo-path
--plan-path
--prompt-source-path
--codex-session-id
--cursor-command
--cursor-model
--cursor-output-format
--codex-command
--codex-review-model
--review-skill
--max-review-iterations
--cursor-timeout-minutes
--codex-timeout-minutes
--output
```

Read the exact initial Cursor prompt from stdin.

Reject empty stdin.

### Repository Discovery and Validation

Canonicalize and validate:

- repository root;
- Git common directory;
- worktree Git directory;
- current branch;
- current HEAD;
- plan path;
- prompt source path;
- project config path.

Require all repository files passed to `prepare` to resolve within the repository root.

Reject symlink escapes.

Run preflight commands using direct subprocess argument arrays:

```text
git rev-parse --show-toplevel
git rev-parse --git-common-dir
git rev-parse --git-dir
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --porcelain=v2 --untracked-files=all
git diff --cached --name-only
```

### Worktree Safety

With `workflow.require_clean_worktree: true`:

- reject any pre-existing staged changes;
- reject dirty tracked or untracked files except:
  - the selected plan file;
  - ignored prompt files;
  - explicitly configured safe paths if support is added;
- require the repository configuration to be committed or otherwise clean;
- store the complete baseline status.

This safety rule exists because the loop later uses stage mode `all`.

Do not silently include unrelated pre-existing work.

### Run Identity

Generate a globally unique run ID:

```text
<project-slug>-<UTC-basic-timestamp>-<short-random-id>
```

Example:

```text
parish360-platform-20260704T134512Z-8f43c1
```

Do not use mutable branch names as the only identifier.

### Run Directory

Create:

```text
$XDG_STATE_HOME/ai_dev_loop/runs/<project-slug>/<run-id>/
```

with this structure:

```text
<run-id>/
├── state.json
├── effective-config.yaml
├── source-config.yaml
├── manifest.json
├── plan/
│   ├── plan.md
│   └── metadata.json
├── prompts/
│   ├── cursor-initial.txt
│   └── fixes/
├── cursor/
│   ├── chat.json
│   └── iterations/
├── codex/
│   ├── reviews/
│   └── events/
├── git/
│   ├── baseline-status.txt
│   ├── status/
│   └── diffs/
├── logs/
│   ├── ai_dev_loop.log
│   └── events.jsonl
└── locks/
```

The internal prompt file name may be normalized to `cursor-initial.txt`; retain the original source filename and path in metadata.

`logs/ai_dev_loop.log` is the human-readable text log. `logs/events.jsonl` is the append-only structured orchestrator event log used for debugging, recovery, and tests.

### Snapshots and Hashes

Copy and hash:

- approved plan;
- exact stdin prompt;
- source configuration;
- effective configuration.

Use SHA-256 and store hashes in `state.json` and `manifest.json`.

At `start`, verify that:

- repository path still exists;
- branch is unchanged;
- HEAD is unchanged;
- current plan content still matches the prepared hash;
- current prompt source content still matches the prepared prompt hash when the source file still exists;
- no unexpected worktree changes appeared.

If the plan or prompt changed after `prepare`, fail safely and instruct the user to prepare a new run. Do not silently refresh the contract.

### Prepare Output

Human output must be concise.

JSON output must be equivalent to:

```json
{
  "schema_version": 1,
  "status": "prepared",
  "run_id": "parish360-platform-20260704T134512Z-8f43c1",
  "project": "parish360-platform",
  "start_command": "ai_dev_loop start parish360-platform-20260704T134512Z-8f43c1",
  "requires_codex_exit": true
}
```

`prepare` must make clear that the user must exit the active Codex TUI before starting the run.

## Run State Schema

Use a versioned JSON schema and typed model.

`state.json` must be equivalent in scope to:

```json
{
  "schema_version": 1,
  "run_id": "parish360-platform-20260704T134512Z-8f43c1",
  "project": {
    "name": "parish360-platform"
  },
  "status": "prepared",
  "created_at": "2026-07-04T13:45:12Z",
  "updated_at": "2026-07-04T13:45:12Z",
  "repository": {
    "root": "/home/user/src/parish360-platform",
    "git_common_dir": "/home/user/src/parish360-platform/.git",
    "git_dir": "/home/user/src/parish360-platform/.git",
    "branch": "feature/active-organization",
    "initial_head": "a43c09d000000000000000000000000000000000",
    "baseline_status_path": "git/baseline-status.txt"
  },
  "plan": {
    "repository_path": "docs/plans/phase-4-active-organization-request-context.md",
    "snapshot_path": "plan/plan.md",
    "sha256": "<sha256>"
  },
  "prompt": {
    "source_repository_path": "docs/plans/prompt_phase-4-active-organization-request-context.txt",
    "snapshot_path": "prompts/cursor-initial.txt",
    "sha256": "<sha256>"
  },
  "codex": {
    "command": "codex",
    "session_id": "019abc00-0000-0000-0000-000000000000",
    "session_model": "model-reported-by-hook",
    "review_model": "configured-review-model",
    "review_skill": "review-staged-cursor-execution",
    "sandbox": "workspace-write"
  },
  "cursor": {
    "command": "agent",
    "model": "composer-2.5-fast",
    "output_format": "stream-json",
    "force": true,
    "trust_workspace": true,
    "sandbox": "disabled",
    "chat_id": null
  },
  "workflow": {
    "max_review_iterations": 3,
    "current_review_iteration": 0,
    "stage_mode": "all",
    "cursor_timeout_minutes": 90,
    "codex_timeout_minutes": 90
  },
  "iterations": [],
  "result": null,
  "last_error": null
}
```

### Iteration Object

Each review iteration must include at least:

```json
{
  "number": 1,
  "kind": "initial_implementation",
  "started_at": "2026-07-04T13:50:00Z",
  "completed_at": "2026-07-04T14:10:00Z",
  "cursor": {
    "prompt_path": "prompts/cursor-initial.txt",
    "events_path": "cursor/iterations/01/events.jsonl",
    "final_message_path": "cursor/iterations/01/final.txt",
    "exit_code": 0
  },
  "git": {
    "status_before_path": "git/status/01-before.txt",
    "status_after_path": "git/status/01-after.txt",
    "staged_diff_path": "git/diffs/01.patch"
  },
  "codex": {
    "events_path": "codex/events/01.jsonl",
    "result_path": "codex/reviews/01.json",
    "report_path": "codex/reviews/01.md",
    "exit_code": 0
  },
  "review": {
    "has_actionable_findings": true,
    "findings_count": 2,
    "highest_severity": "P1",
    "tests_status": "skipped_findings_present"
  }
}
```

Use relative paths for files inside the run directory.

### Status State Machine

Support at least:

```text
prepared
validating
running_cursor
staging
reviewing
waiting_for_cursor_fix
completed
completed_with_residual_risk
max_iterations_reached
interrupted
failed
aborted
```

Define allowed transitions explicitly and test them.

Do not allow arbitrary status mutation.

## Locking and Concurrency

Implement:

- one lock per run;
- one lock per repository worktree.

Prevent two loops from mutating the same worktree concurrently.

Use OS-level file locking appropriate to WSL/Linux.

Store lock metadata with:

- PID;
- run ID;
- repository path;
- start timestamp.

Detect stale locks carefully. Do not remove a live process's lock.

`status` and `inspect` must remain usable while a run lock is held.

## Start Command and Complete Automated Loop

`start <run-id>` performs the complete loop.

### Start Preflight

Before invoking an agent:

- acquire run and repository locks;
- verify prepared state;
- verify hashes, branch, HEAD, and worktree baseline;
- verify `agent`, `codex`, and `git` executables;
- verify Cursor authentication with a non-destructive command when possible;
- verify Codex authentication with a non-destructive command when possible;
- verify configured Cursor model by querying `agent models` or `agent --list-models`;
- verify the Cursor model exact identifier exists;
- validate timeouts;
- ensure the original Codex session ID is present;
- warn clearly that the original interactive Codex TUI must be closed;
- fail before editing if preflight is not satisfied.

Do not rely on `--resume --last` for either agent.

## Cursor Runner

### Create the Chat

For the first implementation:

```text
agent create-chat
```

Capture and validate the returned chat ID.

Store it immediately in `state.json` and `cursor/chat.json` using an atomic write.

If chat creation succeeds but a later step fails, `resume` must reuse the stored ID rather than creating another chat.

### Initial Cursor Execution

Execute Cursor headlessly using an argument array equivalent to:

```text
agent
-p
--force
--trust
--workspace
<repo-root>
--resume
<cursor-chat-id>
--model
<cursor-model>
--output-format
stream-json
<exact-prompt-content>
```

Map optional sandbox configuration only to officially supported flags.

Do not assume Cursor accepts the prompt on stdin. Pass it as one subprocess argument without shell interpolation.

Capture:

- stdout JSONL;
- stderr;
- exit code;
- elapsed time;
- final result text;
- timeout state.

Always persist the Cursor turn artifacts under `cursor/iterations/<nn>/`, including:

- `events.jsonl` with raw Cursor `stream-json` output;
- `stderr.txt`;
- `metadata.json` with redacted arguments, exit code, elapsed time, timeout state, and parse status;
- `final.txt`.

Create `final.txt` for every Cursor turn. If no final result text can be extracted, write an empty file or a short explicit extraction placeholder and record the parse status in `metadata.json`; do not omit the artifact solely because parsing did not find a final message.

Use `start_new_session` or equivalent process-group handling so timeout and abort can terminate the complete child process tree.

### Cursor Corrections

When Codex returns findings, send `cursor_fix_prompt` to the exact same chat:

```text
agent -p ... --resume <same-chat-id> <cursor-fix-prompt>
```

Never create a second Cursor chat for the same run.

### Cursor Output Handling

Use `stream-json` for progress and auditability.

Parse enough to detect:

- initialization;
- tool-call progress;
- result;
- explicit errors;
- completion.

Store the raw JSONL even when parsing fails.

Store enough parsed metadata to distinguish a clean completion, a Cursor-reported error event, and malformed JSONL. Preserve raw output for diagnosis even when the parser cannot understand it.

Never execute text emitted by Cursor.

## Git Staging Runner

After each successful Cursor turn:

1. Record `git status`.
2. Confirm the worktree does not contain unexpected paths relative to the prepared baseline and current iteration.
3. Stage according to configured mode.

For `stage_mode: all`, run:

```text
git add -A
```

using a direct subprocess call.

Prompt files are ignored by the target repository and must not be staged.

Then capture:

```text
git diff --cached --stat
git diff --cached --name-only
git diff --cached
```

Store a patch snapshot for the iteration.

Do not:

- commit;
- create Git tags;
- push;
- reset;
- clean;
- stash;
- unstage user work.

If staging would include paths that violate the baseline safety policy, stop with `failed` and preserve diagnostics.

## Codex Review Runner

### Preserve the Original Session

Every review must execute:

```text
codex exec resume <exact-original-session-id>
```

Never execute a new plain `codex exec` review session.

Never use:

- `--last`;
- a guessed session;
- a subagent as the final reviewer.

Resuming the original session preserves the architectural discussion and plan history.

### Review Invocation

Use:

- repository working directory;
- configured review model;
- configured sandbox;
- JSONL event output;
- final-message output file;
- JSON Schema structured output.

Use a command equivalent to the installed Codex CLI's accepted option order. On the probed Codex CLI, root `codex exec` options such as `--cd` and `--sandbox` must be placed before the `resume` subcommand, while resume-specific options such as `--model`, `--json`, `--output-schema`, and `--output-last-message` follow `resume`:

```text
codex exec
--cd
<repo-root>
--sandbox
workspace-write
resume
--model
<review-model>
--json
--output-schema
<codex-review-result-v1.json>
--output-last-message
<iteration-result.json>
<session-id>
-
```

Supply the review instruction on stdin.

Validate the exact supported option order against the installed Codex CLI and implement the working form. Do not pass `--cd` or `--sandbox` after `resume` unless the local CLI explicitly supports that form.

### Review Prompt

Build a deterministic wrapper prompt that tells the resumed Codex session:

- this is the automated review turn for the existing approved plan;
- invoke the exact configured review skill explicitly, using `$<review-skill>`;
- review current staged changes;
- use the plan snapshot and repository plan path;
- consider the original Cursor prompt;
- consider the latest Cursor final response;
- include the original Cursor prompt content and latest Cursor final response content directly in the review instruction payload, not only as XDG artifact paths;
- include artifact paths for auditability, but do not require the resumed Codex process to read files from the XDG state directory;
- explicitly state when the latest Cursor final response could not be extracted;
- follow the review skill's read-only rule;
- run tests only according to the review skill;
- produce the review report in the skill's required Markdown format;
- return the structured JSON contract;
- when findings exist, create a complete English `cursor_fix_prompt`;
- when no findings exist, set `cursor_fix_prompt` to `null`.

The wrapper must not summarize or reinterpret findings itself.

### Codex Structured Review Schema

Create `codex-review-result-v1.json` equivalent to:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "has_actionable_findings",
    "findings_count",
    "highest_severity",
    "review_markdown",
    "cursor_fix_prompt",
    "tests_status",
    "summary"
  ],
  "properties": {
    "has_actionable_findings": {
      "type": "boolean"
    },
    "findings_count": {
      "type": "integer",
      "minimum": 0
    },
    "highest_severity": {
      "type": ["string", "null"],
      "enum": ["P0", "P1", "P2", "P3", null]
    },
    "review_markdown": {
      "type": "string",
      "minLength": 1
    },
    "cursor_fix_prompt": {
      "type": ["string", "null"]
    },
    "tests_status": {
      "type": "string",
      "enum": [
        "passed",
        "failed",
        "skipped_findings_present",
        "blocked_environment",
        "not_applicable"
      ]
    },
    "summary": {
      "type": "string",
      "minLength": 1
    }
  }
}
```

Add cross-field application validation:

- `findings_count > 0` when `has_actionable_findings` is true.
- `findings_count == 0` when false.
- `highest_severity` is non-null when findings exist.
- `cursor_fix_prompt` is non-empty when findings exist.
- `cursor_fix_prompt` is null when no findings exist.
- `review_markdown` follows the project review skill's expected headings as far as can be validated without coupling to one project.

The orchestrator must decide the loop outcome from the schema-validated structured JSON fields, not by parsing Markdown. `review_markdown` is the human review report and may be checked for broad heading compatibility, but it is not the source of truth for findings extraction, iteration decisions, or correction prompt forwarding.

Store:

- raw JSONL events;
- final structured JSON;
- extracted Markdown report;
- exact Cursor correction prompt.

### Review Skill Compatibility

Project review skills may continue to define the human Markdown review format. For `ai_dev_loop`, they must also allow the final Codex answer to be returned as schema-constrained JSON, with the Markdown report placed in `review_markdown`.

If a repository review skill currently requires "Markdown only" or otherwise forbids structured output, update that project skill before enabling automated review. The generic `ai_dev_loop` wrapper must not fall back to scraping findings from Markdown when structured output is invalid.

### Same Codex Session Generates the Fix Prompt

This is mandatory.

The orchestrator must not generate a generic correction prompt from findings.

The resumed original Codex session must create `cursor_fix_prompt` in the same review turn, using all context available in that session.

`ai_dev_loop` only validates and forwards the returned string.

## Loop Semantics

`workflow.max_review_iterations` counts Codex review passes, including the first review after the initial Cursor implementation.

For a value of `3`, the maximum sequence is:

```text
Cursor initial implementation
Review 1
Cursor correction 1, if needed
Review 2
Cursor correction 2, if needed
Review 3
Stop
```

If Review 3 still contains findings:

- do not invoke Cursor again;
- set status to `max_iterations_reached`;
- leave the latest changes staged;
- preserve the latest review and correction prompt;
- print a clear manual follow-up summary.

### Completion

When Codex returns no actionable findings:

- if tests passed, set `completed`;
- if the review reports environmental test blockage or explicit residual risk without findings, set `completed_with_residual_risk`;
- leave changes staged;
- do not commit or tag;
- print plan path, run ID, iteration count, review path, and final status.

## Resume and Recovery

`resume <run-id>` must recover interrupted runs idempotently.

Use state and artifact existence to determine the next safe action.

Examples:

- chat ID exists, but initial Cursor output is incomplete: retry the same Cursor chat turn only after marking the prior attempt.
- Cursor completed, but staging did not: stage after validating current worktree.
- staging completed, but Codex review did not: resume the exact Codex session and review.
- review completed with findings, but correction was not sent: send the stored correction prompt to the same Cursor chat.
- process was killed during state write: recover from atomic previous state or manifest.
- child timed out: record timeout and require explicit resume.

Do not repeat a completed agent turn merely because the parent process did not update a later state field. Use durable checkpoints.

## Abort

`abort <run-id>` must:

- request termination of an active child process group;
- mark the run `aborted`;
- release locks;
- preserve repository contents and staged changes;
- not reset, clean, stash, or delete agent edits;
- leave enough information for manual inspection.

## Status, Logs, Inspect, and List

### `status`

Show:

- status;
- repository;
- branch and initial HEAD;
- current iteration;
- maximum iterations;
- Cursor chat ID;
- Codex session ID in shortened form;
- active child process;
- latest error;
- next safe action.

### `logs`

Support:

```text
ai_dev_loop logs <run-id>
ai_dev_loop logs <run-id> --follow
ai_dev_loop logs <run-id> --component cursor
ai_dev_loop logs <run-id> --component codex
```

`ai_dev_loop logs <run-id>` should default to the human-readable `logs/ai_dev_loop.log`. Component logs may render the relevant artifact tree, including Cursor and Codex JSONL streams.

Maintain `logs/events.jsonl` as a structured append-only event stream. Each event must include at least:

- `schema_version`;
- UTC timestamp;
- level;
- component;
- event name;
- run ID;
- status when known;
- iteration when known;
- artifact path when relevant;
- redacted detail fields.

Do not include full prompts, authentication payloads, full process environments, or unredacted secrets in structured log detail fields. `--follow` should stream the human log by default and support component logs where practical.

### `inspect`

Show artifact paths and optionally render:

- effective config;
- plan metadata;
- prompt metadata;
- iteration summaries;
- final review.

Do not print complete sensitive prompts by default; require an explicit flag.

### `list`

List recent runs across projects with filters for:

- project;
- status;
- repository;
- date.

## Doctor Command

`doctor` must verify:

- Python/runtime version;
- state/config/cache directory permissions;
- `git`;
- Cursor CLI executable and version;
- Cursor authentication status;
- available Cursor models;
- Codex CLI executable and version;
- Codex authentication status when safely detectable;
- Codex hook installation;
- Codex hook trust status when detectable;
- global skill installation;
- JSON schema availability;
- target repository configuration when `--repo` is supplied.

Produce actionable fixes without modifying configuration unless an explicit install or repair flag is supplied.

## Atomic Persistence

All state writes must be atomic:

1. write to a temporary file in the same directory;
2. flush;
3. fsync where practical;
4. rename over the target.

Maintain:

- `state.json`;
- an append-only structured event log at `logs/events.jsonl`;
- a human-readable text log at `logs/ai_dev_loop.log`;
- enough checkpoints to recover after termination.

Do not write partially valid JSON.

## Process Execution Rules

- Never use `shell=True`.
- Pass subprocess arguments as arrays.
- Capture stdout and stderr separately.
- Stream progress to logs.
- Redact known secret patterns and sensitive environment values.
- Do not log the complete process environment.
- Do not log API keys.
- Set explicit working directories.
- Apply timeouts.
- Terminate process groups, not only parent PIDs.
- Preserve raw outputs for diagnosis after redaction.
- Treat agent output as untrusted data.
- Never evaluate or execute text returned by an agent.

## Security and Privacy

- State is local and user-only.
- Do not upload run artifacts.
- Do not copy Codex transcript content.
- Store only transcript path metadata supplied by the hook.
- Shorten session IDs in normal human output.
- Full IDs remain in `state.json` with `0600` permissions.
- Prompt and review content may contain proprietary code context; protect accordingly.
- Do not store authentication tokens.
- Document retention and provide a safe manual cleanup command or policy.

## Installation and Integration Commands

Implement:

```text
ai_dev_loop integrations install
```

It must:

- install or link the global skill;
- install the SessionStart hook script;
- merge hook configuration;
- create XDG directories;
- report trust instructions;
- be idempotent.

Implement:

```text
ai_dev_loop integrations uninstall
```

It must:

- remove only assets installed by `ai_dev_loop`;
- preserve other hooks and skills;
- preserve run history by default;
- optionally remove state only with an explicit destructive flag and confirmation.

Implement:

```text
ai_dev_loop integrations status
```

It must report exact installed paths and whether files match the current package version.

## Tests

### Unit Tests

Cover:

- XDG path resolution;
- permission application;
- YAML validation;
- configuration precedence;
- run ID generation;
- state schema serialization;
- state transitions;
- atomic writes;
- hashes;
- path containment and symlink escape rejection;
- lock acquisition and stale-lock behavior;
- prompt naming metadata;
- Codex review cross-field validation;
- Cursor JSONL parsing;
- Codex JSONL parsing;
- structured orchestrator event logging;
- redaction;
- hook input/output;
- hook JSON merge and uninstall.

### Integration Tests

Use fake executables placed earlier in `PATH` for:

- `agent`;
- `codex`;
- `git` where appropriate.

Simulate:

1. successful initial implementation and no findings;
2. one findings cycle then success;
3. findings through the maximum iteration;
4. Cursor failure;
5. Cursor timeout;
6. Codex failure;
7. invalid Codex structured output;
8. changed plan after prepare;
9. unexpected dirty worktree;
10. interrupted process and resume;
11. existing Cursor chat reused after failure;
12. same Codex session ID used for every review;
13. same Cursor chat ID used for every implementation and fix;
14. latest Cursor final response content passed into the first Codex review prompt;
15. structured JSON, not Markdown parsing, drives review decisions and fix prompts;
16. hook installation with existing unrelated hooks;
17. uninstall preserving unrelated hooks.

### End-to-End Local Acceptance Test

In a disposable temporary Git repository:

- install the CLI;
- install the global integration into a temporary HOME;
- create a valid `ai_dev_loop.yaml`;
- create a plan and prompt;
- prepare a run through stdin;
- start with fake Cursor and Codex CLIs;
- verify stage-review-fix-review completion;
- verify final staged changes;
- verify run artifacts and state;
- verify no state files were created inside the target repository.

Do not require paid model calls in the automated test suite.

## Documentation

The README must include:

- architecture diagram;
- role definitions;
- installation in WSL;
- `pipx` installation;
- global integration installation;
- hook trust step;
- target repository configuration;
- planning-skill integration;
- prompt-file convention;
- prepare/start handoff;
- complete loop behavior;
- state directory structure;
- status/resume/abort usage;
- logs, structured event logs, and debugging artifacts;
- safety model;
- timeout behavior;
- review skill compatibility with schema-constrained Codex output;
- troubleshooting;
- uninstall;
- privacy and cleanup.

Include an exact user workflow:

```text
1. Start Codex in the target repo.
2. Discuss and finalize the change.
3. Let the project planning skill create the plan and prompt file.
4. Approve the plan.
5. Codex runs ai_dev_loop prepare.
6. Copy the returned start command.
7. Exit Codex with /exit.
8. Run the returned ai_dev_loop start command.
9. Inspect the final staged changes and review report.
```

## Architecture Guardrails

- All agents and code execution remain local.
- The original Codex session is the only authoritative reviewer.
- Every review uses `codex exec resume <exact-session-id>`.
- All Cursor turns use one stored chat ID.
- The orchestrator never regenerates the initial prompt.
- The orchestrator never authors the correction prompt.
- The correction prompt comes from the resumed original Codex session.
- The orchestrator never extracts findings by scraping Markdown review text.
- The Codex structured JSON result is the source of truth for review decisions.
- The target repo never knows the XDG state path.
- The target repo never contains `state.json`, session logs, or agent output.
- The loop never commits, tags, or pushes.
- The loop leaves final changes staged.
- The loop does not use subagents as the final reviewer.
- The loop does not use cloud agents.
- The loop does not run while the original Codex TUI is still expected to own the same active session.
- The system must fail safely on ambiguous state rather than guessing.
- No shell interpolation of prompts, paths, findings, or agent output.
- Do not silently accept unrelated dirty work.
- Preserve all evidence needed for manual recovery.

## Implementation Steps

1. Create the Python package and console entry point.
2. Implement XDG path resolution and secure directory creation.
3. Implement typed project configuration and JSON schemas.
4. Implement typed run state, transitions, atomic writes, event log, and manifests.
5. Implement process execution, timeout, process-group termination, and redaction.
6. Implement Git discovery, baseline validation, staging, and diff capture.
7. Implement `prepare` with stdin prompt persistence, snapshots, hashes, and JSON output.
8. Implement Cursor model validation, chat creation, same-chat execution, JSONL capture, and resume-safe checkpoints.
9. Implement Codex exact-session resume, structured review schema, report extraction, and correction-prompt forwarding.
10. Implement the complete bounded loop.
11. Implement locks, interruption handling, resume, and abort.
12. Implement status, list, logs, inspect, doctor, and config validation.
13. Implement the global Codex skill.
14. Implement the SessionStart hook and safe hook registration.
15. Implement integration install, status, repair behavior, and uninstall.
16. Add comprehensive unit and integration tests.
17. Add the disposable end-to-end acceptance test.
18. Write the full README and troubleshooting documentation.
19. Run formatting, linting, type checking, unit tests, integration tests, and package build validation.
20. Verify installation and command discovery inside WSL.

## Validation Commands

Choose concrete tools during implementation and document them. The final repository must provide commands equivalent to:

```bash
uv run python -m pytest
uv run python -m ruff check .
uv run python -m ruff format --check .
uv run python -m mypy src
uv run python -m build
```

Also validate:

```bash
ai_dev_loop --help
ai_dev_loop doctor
ai_dev_loop integrations status
ai_dev_loop config validate --repo <fixture-repo>
```

## Acceptance Criteria

- `ai_dev_loop` installs as a command in WSL.
- `prepare` accepts the prompt through stdin.
- `prepare` persists plan, prompt, configuration, hashes, repository metadata, and exact Codex session ID.
- `prepare` returns a start command and does not start the loop.
- The global Codex skill is installed in `$HOME/.agents/skills`.
- The SessionStart hook exposes the exact session ID to Codex context.
- Hook installation preserves existing hooks and supports safe uninstall.
- Run state is stored under XDG state outside target repositories.
- Cursor is run in local headless mode.
- One Cursor chat is created and reused.
- Cursor's initial and fix prompts are persisted.
- Changes are staged after every Cursor turn.
- Every review resumes the exact original Codex session.
- The configured project review skill is explicitly invoked.
- Codex returns a schema-validated review decision.
- The same Codex session authors each Cursor correction prompt.
- The loop stops on no findings or at the configured review limit.
- The final changes remain staged.
- The system can recover from interruption without changing agent identity.
- No cloud agents, commits, tags, or pushes are used.
- Unit, integration, and disposable end-to-end tests pass.
- Documentation is sufficient for installation and daily use.

## Reference Documents

### Cursor

- CLI overview: https://cursor.com/docs/cli/overview
- CLI parameters: https://cursor.com/docs/cli/reference/parameters
- CLI slash commands: https://cursor.com/docs/cli/reference/slash-commands
- CLI configuration: https://cursor.com/docs/cli/reference/configuration
- Headless CLI: https://cursor.com/docs/cli/headless
- Using Agent in CLI: https://cursor.com/docs/cli/using
- Output formats: https://cursor.com/docs/cli/reference/output-format

### Codex

- CLI features and resume: https://developers.openai.com/codex/cli/features
- Non-interactive mode: https://developers.openai.com/codex/noninteractive
- CLI options: https://developers.openai.com/codex/cli/reference
- Hooks: https://developers.openai.com/codex/hooks
- Skills: https://developers.openai.com/codex/skills

## OpenQuestions

None. The implementation must verify exact local CLI flag behavior and valid model slugs against the installed Cursor and Codex versions rather than inventing unsupported values.
