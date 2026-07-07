# Phase 7 Findings

This file summarizes the Phase 7 implementation, review cycle, and real local installation. It is intended as a handoff artifact so later work can proceed without depending on prior chat history.

## Scope

Phase 7 implemented the global Codex integration layer for `ai_dev_loop`.

The implemented slice makes these commands real:

- `ai_dev_loop integrations install`
- `ai_dev_loop integrations uninstall`
- improved `ai_dev_loop integrations status`
- improved `ai_dev_loop doctor` integration checks

Phase 7 added user-level Codex assets that allow a Codex session to identify its exact session ID and hand off an approved implementation plan to `ai_dev_loop`:

- global handoff skill under `$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md`;
- standalone SessionStart hook script under `$HOME/.codex/hooks/ai_dev_loop_session_start.py`;
- safe SessionStart hook registration in `$HOME/.codex/hooks.json`;
- minimal Codex session metadata under XDG state at `codex-sessions/<session-id>.json`.

Phase 7 does not change:

- the bounded Cursor/Codex review-fix loop from Phase 5;
- real `resume` behavior from Phase 5;
- real `abort` behavior from Phase 6;
- Cursor chat identity rules;
- Codex session identity rules;
- staged-diff safety rules;
- structured-review decision rules.

Phase 7 also does not bypass Codex hook trust. The user must still open `/hooks` in Codex and trust the `ai_dev_loop` hook when prompted.

## Environment And Tooling

- Workspace: `/home/rojobad/Projects/ai_dev_loop`
- Primary runtime workflow: `uv`
- Package runtime requirement: Python `>=3.11`
- Automated tests use temporary `HOME` and XDG directories for integration install/status/uninstall coverage.
- Automated tests do not invoke real Cursor or Codex model activity.
- The user still prefers not to modify or replace system Python.

Recommended validation commands remain:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s
uv run python -m build
uv run ai_dev_loop --help
uv run ai_dev_loop integrations status
uv run ai_dev_loop doctor
```

The implementing agent reported final validation passing with:

```text
219 passed
ruff clean
mypy clean
```

Earlier Phase 7 validation also reported:

```text
python -m build clean
CLI smoke tests clean
```

The staged review also ran:

```bash
git diff --cached --check
```

No whitespace errors were reported.

## Delivered Files And Structure

Phase 7 added or materially updated:

- `plans/phase-7-global-codex-integrations.md`: Phase 7 implementation plan.
- `plans/prompt_phase-7-global-codex-integrations.txt`: concise Cursor handoff prompt.
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`: global integration governance rule. It is tracked and should stay aligned with future integration changes.
- `src/ai_dev_loop/integrations/__init__.py`: integration package marker.
- `src/ai_dev_loop/integrations/codex/__init__.py`: Codex integration package marker.
- `src/ai_dev_loop/integrations/codex/assets.py`: package asset loading and content comparison helpers.
- `src/ai_dev_loop/integrations/codex/hooks_json.py`: safe `~/.codex/hooks.json` merge, status, backup, and uninstall helpers.
- `src/ai_dev_loop/integrations/codex/install.py`: install, uninstall, status rendering, and doctor integration checks.
- `src/ai_dev_loop/integrations/codex/paths.py`: global integration path helpers.
- `src/ai_dev_loop/integrations/codex/session_start.py`: standalone Codex SessionStart hook script.
- `src/ai_dev_loop/integrations/codex/skill/SKILL.md`: package-owned global handoff skill template.
- `src/ai_dev_loop/integrations/codex/skill/__init__.py`: skill package marker so package resources can load `SKILL.md`.
- `src/ai_dev_loop/commands/integrations.py`: now delegates to real integration install/status/uninstall implementation.
- `src/ai_dev_loop/commands/doctor.py`: now includes global integration checks.
- `src/ai_dev_loop/cli.py`: wires `integrations install --output json` and `integrations uninstall --output json`.
- `README.md`: documents the full Phase 7 integration workflow, trust step, uninstall, and privacy behavior.
- `tests/integration/test_integrations.py`: integration coverage for global install/status/uninstall using temporary `HOME`.
- `tests/unit/test_codex_integration_assets.py`: package asset and wheel inclusion coverage.
- `tests/unit/test_codex_integration_hook_script.py`: SessionStart hook input/output, privacy, path safety, and permissions coverage.
- `tests/unit/test_codex_integration_hooks_json.py`: hook JSON merge, duplicate cleanup, stale-registration handling, and uninstall-preservation coverage.
- `tests/conftest.py`: temporary HOME fixtures for integration tests.
- `tests/unit/test_process.py` and `tests/integration/test_abort.py`: formatting-only updates from the staged diff.

## CLI Surface

Implemented by the end of Phase 7:

- `ai_dev_loop prepare`
- `ai_dev_loop start <run-id>`
- `ai_dev_loop resume <run-id>`
- `ai_dev_loop abort <run-id>`
- `ai_dev_loop config validate`
- `ai_dev_loop status <run-id>`
- `ai_dev_loop list`
- `ai_dev_loop logs <run-id>`
- `ai_dev_loop logs <run-id> --component cursor`
- `ai_dev_loop logs <run-id> --component codex`
- `ai_dev_loop inspect <run-id>`
- `ai_dev_loop doctor`
- `ai_dev_loop integrations install`
- `ai_dev_loop integrations install --output json`
- `ai_dev_loop integrations uninstall`
- `ai_dev_loop integrations uninstall --output json`
- `ai_dev_loop integrations status`
- `ai_dev_loop integrations status --output json`

No required commands remain placeholders after Phase 7.

## Global Installation Behavior

`ai_dev_loop integrations install` now:

1. Ensures app XDG directories exist.
2. Installs or updates the global handoff skill:

   ```text
   $HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md
   ```

3. Installs or updates the standalone SessionStart hook script:

   ```text
   $HOME/.codex/hooks/ai_dev_loop_session_start.py
   ```

4. Loads and validates:

   ```text
   $HOME/.codex/hooks.json
   ```

5. Safely merges an idempotent SessionStart hook registration:

   ```json
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
   ```

6. Preserves unrelated top-level keys, unrelated hook events, and unrelated SessionStart entries.
7. Backs up an existing `hooks.json` before writing changes.
8. Writes sensitive files with restrictive permissions where supported.
9. Reports whether each asset was `created`, `updated`, or `current`.
10. Prints the trust instruction:

   ```text
   Open /hooks in Codex and trust the ai_dev_loop hook. The hook may be skipped until it is trusted.
   ```

The installer fails safely on invalid `hooks.json` and does not overwrite it.

## Global Uninstall Behavior

`ai_dev_loop integrations uninstall` now:

1. Loads and validates `hooks.json` before deleting installed files.
2. Computes the hook registration update.
3. Deletes only the installed `SKILL.md` file.
4. Removes the `ai-dev-loop-handoff` skill directory only if it becomes empty.
5. Deletes only the installed hook script file.
6. Removes only the exact installed `ai_dev_loop` hook registration from `hooks.json`.
7. Preserves unrelated hooks and unrelated skills.
8. Backs up `hooks.json` before writing changes.
9. Preserves run history, prompts, reviews, staged patches, logs, and XDG state by default.

Uninstall does not delete run artifacts or perform destructive repository cleanup.

## Integration Status And Doctor

`ai_dev_loop integrations status` now reports:

- skill path;
- skill installed;
- skill matches current package content;
- hook script path;
- hook script installed;
- hook script matches current package content;
- hooks JSON path;
- hooks JSON exists;
- hooks JSON valid;
- hook registration present;
- duplicate hook registration count;
- hook registration command;
- hook trust status;
- remediation messages.

Hook trust status is reported as `unknown` because it is not safely detectable from local files. This is expected. The actionable instruction is to open `/hooks` in Codex and trust the hook.

`ai_dev_loop doctor` now includes non-mutating integration checks for:

- `integration_skill`;
- `integration_hook_script`;
- `integration_hooks_json`;
- `integration_hook_registration`;
- `integration_hook_trust`;
- `integration_remediation`.

## SessionStart Hook Behavior

The installed hook script is standalone and standard-library-only.

It accepts only:

- `hook_event_name == "SessionStart"`;
- `source` in:
  - `startup`;
  - `resume`;
  - `clear`;
  - `compact`;
- UUID-formatted `session_id`.

The hook extracts only:

- `session_id`;
- `model`;
- `cwd`;
- `transcript_path`;
- `source`.

It writes minimal session metadata under:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

or, when `XDG_STATE_HOME` is unset:

```text
~/.local/state/ai_dev_loop/codex-sessions/<session-id>.json
```

The hook response includes:

```json
{
  "hookSpecificOutput": {
    "additionalContext": "..."
  }
}
```

The additional context tells Codex:

- `ai_dev_loop` integration is active;
- the exact current Codex session ID;
- the current Codex model;
- the current working directory;
- to pass this exact session ID when preparing an approved run;
- not to infer another session;
- not to use `--last`.

The hook:

- never reads transcript contents;
- never copies transcript contents;
- never stores auth material;
- exits safely with valid JSON on invalid or incomplete input;
- rejects unsafe session IDs before writing files or returning session context;
- applies `0700` to the `ai_dev_loop` state root and session directories where supported;
- applies `0600` to session record files where supported.

## Global Handoff Skill Behavior

The installed skill is named:

```text
ai-dev-loop-handoff
```

It instructs Codex to:

1. Confirm the implementation plan is final and approved.
2. Confirm the separate Cursor prompt file exists and matches the approved plan.
3. Read the target repository's `ai_dev_loop.yaml`.
4. Use the exact current Codex session ID from SessionStart context.
5. Run `ai_dev_loop prepare`.
6. Pass the exact approved Cursor prompt through stdin.
7. Prefer `--output json`.
8. Parse the prepare result.
9. Never run `ai_dev_loop start` from the active Codex TUI.
10. Tell the user to exit Codex with `/exit`.
11. Tell the user to run the returned `start_command` in the shell.

The skill includes recovery guidance when SessionStart context is missing:

- run `ai_dev_loop integrations status`;
- trust the hook in `/hooks`;
- restart or resume Codex;
- pass `--codex-session-id` manually only if the exact session ID is known from a trusted source;
- never guess, shorten, or substitute another session ID.

## Hook JSON Ownership Rules

`src/ai_dev_loop/integrations/codex/hooks_json.py` intentionally distinguishes install/merge behavior from uninstall behavior.

Install/merge:

- removes exact current `ai_dev_loop` hook registrations;
- removes stale managed registrations only when they have a strong ownership signal:
  - command has the canonical `/.codex/hooks/ai_dev_loop_session_start.py` suffix; and
  - `statusMessage` equals `Loading ai_dev_loop session context`;
- preserves unrelated same-named hooks when their metadata does not match `ai_dev_loop` ownership.

Uninstall:

- removes only the exact expected installed command;
- does not remove stale same-named hooks elsewhere;
- preserves unrelated hooks, including same-named scripts at different paths.

This distinction was added after review to avoid deleting unrelated hooks while still allowing install to update old `ai_dev_loop` registrations.

## Real Installation Performed

The user explicitly approved real global installation during Phase 7.

After implementation, the install command was run against the user's real HOME:

```bash
uv run ai_dev_loop integrations install
```

The reported output was:

```text
ai_dev_loop integrations install
Skill: current (/home/rojobad/.agents/skills/ai-dev-loop-handoff/SKILL.md)
Hook script: updated (/home/rojobad/.codex/hooks/ai_dev_loop_session_start.py)
hooks.json: current (/home/rojobad/.codex/hooks.json)
Open /hooks in Codex and trust the ai_dev_loop hook. The hook may be skipped until it is trusted.
```

Installed paths:

```text
/home/rojobad/.agents/skills/ai-dev-loop-handoff/SKILL.md
/home/rojobad/.codex/hooks/ai_dev_loop_session_start.py
/home/rojobad/.codex/hooks.json
```

Hook trust remains a manual Codex UI action. The user still needs to open `/hooks` in Codex and trust the `ai_dev_loop` hook if Codex has not already trusted it.

## Review Findings Fixed During Phase 7

Several staged-review findings were valid and fixed. These are important because future integration refactors may accidentally reintroduce them.

### P1: `session_id` path traversal in SessionStart hook

Initial code accepted any non-empty string as `session_id` and used it directly as:

```text
codex-sessions/<session-id>.json
```

Risk:

- A malformed hook payload containing `/` or `..` could write outside `codex-sessions`.

Fix:

- Added UUID-format validation with `SESSION_ID_RE`.
- Added `is_safe_session_id()`.
- Rejects unsafe values before writing files or returning session context.
- Added tests for `../escape` and non-UUID `abc`.

### P1: Over-broad hook matching during uninstall

Initial hook ownership detection used substring matching for `ai_dev_loop_session_start.py`.

Risk:

- `install` or `uninstall` could remove unrelated hooks that happened to reference a same-named script elsewhere.

Fix:

- Uninstall now uses exact expected command matching only.
- Added `test_uninstall_preserves_unrelated_same_named_script`.

### P2: Skill directory deletion during uninstall

Initial uninstall used `shutil.rmtree(skill_destination.parent)`.

Risk:

- User-added files under `~/.agents/skills/ai-dev-loop-handoff/` could be deleted.

Fix:

- Uninstall deletes only `SKILL.md`.
- It removes the directory only if it is empty.
- Added `test_uninstall_preserves_user_files_in_skill_directory`.

### P3: Parent XDG directory permissions

Initial hook directory creation applied `0700` only to the final directory.

Risk:

- On first hook execution, parent directories under the hook-owned XDG state path could inherit broader umask-derived permissions.

Fix:

- `ensure_dir()` now applies `0700` to the `ai_dev_loop` root and every subdirectory under it.
- `atomic_write_json()` uses `ensure_dir(path.parent)`.
- Permission tests now assert both `ai_dev_loop` and `codex-sessions` directories are `0700`.

### P2: Invalid `hooks.json` during uninstall removed installed assets first

Initial uninstall deleted the skill and hook script before parsing `hooks.json`.

Risk:

- Invalid `hooks.json` left a broken hook registration pointing at a missing script.

Fix:

- Uninstall now loads and validates `hooks.json` first.
- It computes the hook update before removing installed files.
- It removes files only after validation succeeds.
- It writes updated `hooks.json` last.
- Added `test_invalid_hooks_json_uninstall_leaves_installed_files`.

### P2: Install merge removed unrelated same-named canonical hooks

Initial managed merge removed any command ending in:

```text
/.codex/hooks/ai_dev_loop_session_start.py
```

Risk:

- Unrelated hooks with a same-named script and different metadata could be removed.

Fix:

- Managed matching now requires a stronger ownership signal:
  - exact expected command; or
  - canonical suffix plus `statusMessage == "Loading ai_dev_loop session context"`.
- Uninstall still uses exact command matching only.
- Updated stale-registration test to include `statusMessage`.
- Added `test_merge_preserves_unrelated_same_named_canonical_hook`.

The final staged review after these fixes reported no actionable findings.

## Tests

The final suite reportedly passed with `219` tests.

New or heavily updated Phase 7 coverage includes:

- package asset loading;
- built wheel includes integration assets;
- first-time install in temporary `HOME`;
- idempotent second install;
- install with unrelated hooks;
- invalid `hooks.json` install fail-safe behavior;
- invalid `hooks.json` uninstall fail-safe behavior;
- uninstall preserving unrelated hooks;
- uninstall preserving unrelated same-named hook scripts;
- install merge preserving unrelated same-named canonical hooks;
- stale managed hook registration update;
- duplicate hook cleanup;
- `integrations status --output json` after install and uninstall;
- CLI install/uninstall JSON output;
- `doctor --output json` integration checks;
- installed file permissions;
- skill content package match;
- uninstall preserving user files in the skill directory;
- hook handling of valid SessionStart sources;
- hook handling of invalid or incomplete input;
- hook transcript privacy;
- hook session record permissions;
- hook rejection of unsafe session IDs.

During review, two targeted temporary simulations were also run:

- invalid `hooks.json` during uninstall now raises while leaving installed files intact;
- unrelated same-named canonical hooks are preserved during install merge while the expected hook is added.

## Privacy And Output

Phase 7 preserves the privacy posture from earlier phases:

- default CLI output does not print full prompts;
- default CLI output does not print fix prompts;
- default CLI output does not print staged patches;
- default CLI output does not print raw Codex JSONL;
- default CLI output does not print transcript contents;
- default CLI output does not print auth payloads;
- hook session records store only minimal metadata;
- hook trust status is not guessed from local files;
- sensitive integration files are written with `0600` where supported;
- integration directories are written with `0700` where supported.

## Important Invariants For Future Agents

- Do not install skills under `~/.codex/skills`; the global handoff skill belongs under `$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md`.
- Do not bypass or mutate Codex hook trust state.
- Do not run `ai_dev_loop start` from the active Codex TUI.
- Do not infer, shorten, guess, or substitute Codex session IDs.
- Do not use `--last` for Codex review or handoff.
- Do not read or copy Codex transcript contents from the hook.
- Do not broaden hook ownership matching without tests proving unrelated hooks are preserved.
- Do not make uninstall remove run history, prompts, reviews, logs, staged patches, or unrelated hooks.
- Do not delete the whole handoff skill directory unless it is empty after removing `SKILL.md`.
- Do not delete installed files before validating `hooks.json` during uninstall.
- Keep the SessionStart hook standalone and standard-library-only.
- Keep package assets available from built wheels and sdists.
- Keep tests isolated with temporary `HOME` and XDG directories.
- Keep bounded loop, resume, and abort behavior separate from integration install/uninstall code.

## Current Repository Notes

At the time this handoff was written, the staged Phase 7 set included:

```text
README.md
plans/phase-7-global-codex-integrations.md
plans/prompt_phase-7-global-codex-integrations.txt
src/ai_dev_loop/cli.py
src/ai_dev_loop/commands/doctor.py
src/ai_dev_loop/commands/integrations.py
src/ai_dev_loop/integrations/__init__.py
src/ai_dev_loop/integrations/codex/__init__.py
src/ai_dev_loop/integrations/codex/assets.py
src/ai_dev_loop/integrations/codex/hooks_json.py
src/ai_dev_loop/integrations/codex/install.py
src/ai_dev_loop/integrations/codex/paths.py
src/ai_dev_loop/integrations/codex/session_start.py
src/ai_dev_loop/integrations/codex/skill/SKILL.md
src/ai_dev_loop/integrations/codex/skill/__init__.py
tests/conftest.py
tests/integration/test_abort.py
tests/integration/test_integrations.py
tests/unit/test_codex_integration_assets.py
tests/unit/test_codex_integration_hook_script.py
tests/unit/test_codex_integration_hooks_json.py
tests/unit/test_process.py
```

The `phase-7-findings.md` handoff file itself may need to be staged separately if the user wants it included with the Phase 7 history.

Repo-local governance files include:

```text
.cursor/rules/ai-dev-loop-governance.mdc
.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc
.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc
.cursor/rules/ai-dev-loop-codex-review-contracts.mdc
.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc
.cursor/rules/ai-dev-loop-abort-contracts.mdc
.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc
.agents/skills/create-cursor-plan/SKILL.md
.agents/skills/review-staged-changes/SKILL.md
```

No `AGENTS.md` file is present.

Earlier Windows metadata artifact `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` should still be treated as unrelated and not modified unless explicitly requested.

## Known Pending Work

The main product implementation described by the master plan is now functionally complete through Phase 7.

Remaining work is mostly validation, packaging, documentation polish, and acceptance-hardening:

- ensure the user trusts the hook in Codex `/hooks`;
- restart or resume Codex after trust so SessionStart context is injected;
- run a disposable end-to-end acceptance test with fake Cursor/Codex CLIs if not already done in final acceptance;
- verify installation and command discovery after installing the built package outside editable/dev mode;
- keep README and governance rules aligned with any future install/uninstall behavior changes;
- decide whether to add an explicit destructive state-cleanup command or continue documenting manual cleanup only.

## Open Issues

None known from the final staged review pass for Phase 7.
