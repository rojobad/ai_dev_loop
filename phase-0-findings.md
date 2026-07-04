# Phase 0 Findings

This file summarizes the non-destructive local probes run before Phase 1. It is intended as a handoff artifact for a forked agent so Phase 1 can proceed without depending on prior chat history.

## Scope

Phase 0 verified local CLI availability, relevant command shapes, auth/model discovery where safe, and runtime constraints. It did not install anything, modify global Codex hooks, run Cursor implementation prompts, run Codex review prompts, commit, push, or edit repository state outside this handoff.

One approved side-effect probe was run:

- `agent create-chat` was executed once.
- It exited with code `0`.
- It returned a plain UUID on stdout.
- The exact UUID is intentionally not recorded here because it identifies a real empty Cursor chat and is not needed for implementation.

## Environment

- Workspace: `/home/rojobad/Projects/ai_dev_loop`
- OS: Ubuntu 22.04.5 LTS under WSL2
- Kernel string included `microsoft-standard-WSL2`
- `git`: `/usr/bin/git`, version `2.34.1`
- `agent`: `/home/rojobad/.local/bin/agent`, version `2026.07.01-41b2de7`
- `codex`: `/home/rojobad/.local/bin/codex`, version `codex-cli 0.142.5`
- `python3`: version `3.10.12`
- `python`: not present in PATH
- `python3.11`: not present in PATH
- `python3 -m pip`: unavailable because `pip` is not installed for system Python
- `pipx`: not present in PATH
- `uv`: not present in PATH
- `pyenv`: not present in PATH
- `asdf`: not present in PATH
- XDG variables: only `XDG_RUNTIME_DIR` and `XDG_DATA_DIRS` were set; config/state/cache should use documented fallbacks unless the user sets them.

## Python Tooling Decision

The user prefers not to touch or replace system Python.

Phase 1 should use `uv` as the primary Python/runtime workflow for both development and user-level product installation:

```bash
uv python install 3.11
uv venv --python 3.11 .venv
uv sync
uv run python -m pytest
uv tool install .
```

The implementation should still be a standard `pyproject.toml` Python package. `pipx install .` can remain documented as an optional alternative, but `uv` should be the recommended path because it can use a user-managed Python 3.11+ runtime without changing `/usr/bin/python3`.

## Cursor CLI Findings

`agent --help` confirms support for the main workflow flags:

- `-p` / `--print` for headless/script mode.
- `--output-format text|json|stream-json`, only with `--print`.
- `--resume [chatId]`.
- `--model <model>`.
- `--force`.
- `--trust`, only with print/headless mode.
- `--workspace <path-or-name>`.
- `--sandbox enabled|disabled`.
- Prompt content is accepted as positional arguments.

Subcommands verified:

- `agent create-chat` exists and returns a new empty chat ID on stdout.
- `agent models` exists and lists available account models.
- `agent status --format json` exists and reports authentication status.

Auth/model observations:

- `agent status --format json` reported authenticated. Do not store or log personal account fields from this JSON in run artifacts.
- `agent models` listed `composer-2.5-fast`, matching the model in the plan's sample config.

Recommended Cursor execution shape remains:

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
--sandbox
disabled
<exact-prompt-content>
```

The implementation should probe `agent --help`, `agent models`, and `agent status --format json` defensively because CLI behavior can change.

## Codex CLI Findings

`codex --help` confirms:

- `codex exec` exists.
- `codex login status` exists and is a safe auth check.
- `codex doctor --json` exists for diagnostics.
- `codex debug models` exists and emits a very large JSON model catalog.

`codex login status` reported logged in. Do not persist auth details.

`codex exec --help` confirms root exec options:

- `--cd <DIR>`.
- `--sandbox read-only|workspace-write|danger-full-access`.
- `--model <MODEL>`.
- `--json`.
- `--output-schema <FILE>`.
- `--output-last-message <FILE>`.
- Prompt can be read from stdin when prompt argument is `-`.

`codex exec resume --help` confirms:

- Usage: `codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]`.
- An explicit session ID is accepted.
- `--last` exists but must not be used by `ai_dev_loop`.
- Prompt `-` reads from stdin.
- Resume-specific options include `--model`, `--json`, `--output-schema`, and `--output-last-message`.

Important option-order finding:

- `codex exec --cd <repo-root> --sandbox workspace-write resume --help` works.
- `codex exec resume --sandbox workspace-write --help` fails with `unexpected argument '--sandbox'`.
- Therefore the runner must place `--cd` and `--sandbox` before `resume` for the probed Codex CLI.

Recommended Codex review command shape:

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

The implementation should still validate the local CLI's accepted form during `doctor` or preflight and fail with a clear message if flags differ.

`codex debug models` emits a huge JSON payload that includes model metadata and substantial instruction text. `ai_dev_loop doctor` should summarize model availability and avoid logging the full raw catalog by default.

## Codex Integration Files

Current filesystem observations:

- `~/.codex/hooks.json` does not exist yet.
- `~/.agents/skills` does not exist yet.

The integration installer should therefore handle first-time creation, but still implement merge, backup, idempotency, and uninstall preservation for future runs.

## Not Yet Verified

These should be covered by implementation-time tests or later manual probes:

- Running `agent -p ... <prompt>` against a real prompt was not tested to avoid model/tool activity.
- Resuming a real Codex session with `codex exec ... resume <session-id> -` was not tested to avoid model activity.
- Codex hook trust behavior was not tested because no hook was installed.
- Hook JSON merge/uninstall was not tested.
- `uv` itself was not installed during Phase 0.
- No temporary E2E fake-agent/fake-codex acceptance test has been built yet.

## Repository Notes

At the time of Phase 0, the repository was mostly empty:

- `README.md`
- `plan-2-build-ai-dev-loop-orchestrator.md`
- `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier`

The `Zone.Identifier` file is a Windows metadata artifact and should probably remain ignored or be removed only with explicit user approval.
