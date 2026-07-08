# Phase 7.5 Findings

This file summarizes the Phase 7.5 implementation, review cycle, and local validation. It is intended as a handoff artifact so the next agent can proceed to Phase 8 without relying on prior chat history.

## Scope

Phase 7.5 was added after discovering that Codex Desktop on Windows and Codex CLI inside WSL use different Codex homes.

The important topology is:

- Codex Desktop runs as a Windows desktop application.
- Cursor and non-interactive Codex CLI subprocesses run in WSL.
- Target repositories live in WSL.
- Codex Desktop reads and writes the Windows Codex home, for example:

  ```text
  /mnt/c/Users/<windows-user>/.codex
  ```

- Codex CLI in WSL reads and writes the WSL Codex home, for example:

  ```text
  /home/<wsl-user>/.codex
  ```

This means the WSL-only Phase 7 hook install is not visible to Codex Desktop. Trusting a hook in the WSL CLI `/hooks` surface is not sufficient for Codex Desktop.

Phase 7.5 implemented two explicit integration targets:

```text
wsl-cli
codex-desktop-wsl
```

The existing Phase 7 behavior remains the default `wsl-cli` target. The new `codex-desktop-wsl` target installs Codex-visible assets into the Windows Codex-visible home while executing the SessionStart hook through WSL.

Phase 7.5 does not change:

- bounded Cursor/Codex review-fix loop behavior;
- real `resume` behavior;
- real `abort` behavior;
- Cursor chat identity rules;
- Codex session identity rules;
- staged-diff safety rules;
- structured-review decision rules.

Phase 7.5 also does not bypass Codex hook trust. For the desktop target, the user must open `/hooks` in Codex Desktop and trust the `ai_dev_loop` hook there.

## Key Design Decision

Do not share the whole Windows `.codex` home with WSL.

Specifically:

- Do not set WSL `CODEX_HOME` to `/mnt/c/.../.codex`.
- Do not symlink the whole WSL `.codex` home to Windows.
- Do not symlink the whole WSL `.codex/sessions` directory to Windows.
- Do not copy, migrate, open, or inspect Codex SQLite databases across Windows and WSL.

The safe bridge is a nested symlink only:

```text
~/.codex/sessions/from-desktop -> /mnt/c/Users/<windows-user>/.codex/sessions
```

This exposes desktop rollout JSONL files by session ID while leaving each Codex surface on its own SQLite state database.

The official product surface is now:

```text
ai_dev_loop integrations sessions install
ai_dev_loop integrations sessions status
ai_dev_loop integrations sessions list
ai_dev_loop integrations sessions remove
```

The earlier helper files remain untracked reference material:

```text
scripts/codex-desktop-wsl-sessions.md
scripts/share-codex-desktop-sessions.sh
```

They should not be treated as the long-term product interface. Phase 8 may fold the markdown content into final docs. If the shell script is kept later, it should delegate to the Python CLI or preserve identical safety checks.

## Delivered Files And Structure

Phase 7.5 added or materially updated:

- `plans/phase-7-5-codex-desktop-wsl-bridge.md`: Phase 7.5 implementation plan.
- `plans/prompt_phase-7-5-codex-desktop-wsl-bridge.txt`: concise Cursor handoff prompt.
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`: updated target and bridge invariants.
- `README.md`: documents both targets and the safe bridge workflow.
- `src/ai_dev_loop/cli.py`: wires target-aware integration commands and the `integrations sessions` command group.
- `src/ai_dev_loop/commands/doctor.py`: includes desktop target diagnostics when detectable.
- `src/ai_dev_loop/commands/integrations.py`: exports target-aware install/status/uninstall and session bridge commands.
- `src/ai_dev_loop/integrations/codex/desktop_bridge.py`: safe nested session bridge implementation.
- `src/ai_dev_loop/integrations/codex/hooks_json.py`: supports desktop `wsl.exe` hook registrations.
- `src/ai_dev_loop/integrations/codex/install.py`: target-aware install/status/uninstall/rendering/doctor checks.
- `src/ai_dev_loop/integrations/codex/paths.py`: adds Codex home path helper.
- `src/ai_dev_loop/integrations/codex/skill/SKILL.md`: desktop recovery guidance.
- `src/ai_dev_loop/integrations/codex/target.py`: typed integration targets and target context resolution.
- `src/ai_dev_loop/integrations/codex/windows_home.py`: Windows Codex home detection and validation.
- `src/ai_dev_loop/integrations/codex/wsl_invocation.py`: WSL distro detection and desktop hook command generation.
- `tests/conftest.py`: hermetic native-WSL fixtures for Windows-propagated env cases.
- `tests/integration/test_desktop_integrations.py`: CLI and target integration coverage.
- `tests/integration/test_integrations.py`: backward-compatible status JSON assertion.
- `tests/unit/test_codex_desktop_bridge.py`: session bridge unit coverage.
- `tests/unit/test_codex_integration_targets.py`: target, Windows home, WSL invocation, and command generation coverage.

## CLI Surface

The integration commands now support:

```bash
ai_dev_loop integrations install --target wsl-cli
ai_dev_loop integrations uninstall --target wsl-cli
ai_dev_loop integrations status --target wsl-cli

ai_dev_loop integrations install --target codex-desktop-wsl
ai_dev_loop integrations uninstall --target codex-desktop-wsl
ai_dev_loop integrations status --target codex-desktop-wsl
```

Desktop-target options:

```text
--windows-codex-home
--wsl-distro
--wsl-hook-python
--wsl-hook-script-path
--install-session-bridge
```

Session bridge commands:

```bash
ai_dev_loop integrations sessions install
ai_dev_loop integrations sessions status
ai_dev_loop integrations sessions list
ai_dev_loop integrations sessions remove
```

Machine output is available with `--output json` where relevant.

## Target Behavior

### `wsl-cli`

This target preserves Phase 7 behavior.

Installed assets:

```text
/home/<wsl-user>/.agents/skills/ai-dev-loop-handoff/SKILL.md
/home/<wsl-user>/.codex/hooks/ai_dev_loop_session_start.py
/home/<wsl-user>/.codex/hooks.json
```

Hook command shape:

```text
python3 /home/<wsl-user>/.codex/hooks/ai_dev_loop_session_start.py
```

### `codex-desktop-wsl`

Installed assets:

```text
/mnt/c/Users/<windows-user>/.agents/skills/ai-dev-loop-handoff/SKILL.md
/home/<wsl-user>/.codex/hooks/ai_dev_loop_session_start.py
/mnt/c/Users/<windows-user>/.codex/hooks.json
```

Hook command shape:

```text
wsl.exe -d <distro> --exec python3 /home/<wsl-user>/.codex/hooks/ai_dev_loop_session_start.py
```

The skill and `hooks.json` must be visible to Codex Desktop in the Windows home. The hook script itself remains in WSL and writes `ai_dev_loop` state under WSL XDG state.

`integrations install --target codex-desktop-wsl` does not create the desktop session bridge by default. It reports bridge status and prints the next action when the bridge is missing:

```text
ai_dev_loop integrations sessions install
```

The bridge can be created explicitly through:

```bash
ai_dev_loop integrations sessions install
```

or during install only when the explicit flag is supplied:

```bash
ai_dev_loop integrations install --target codex-desktop-wsl --install-session-bridge
```

## Windows Home Resolution

Windows Codex home resolution lives in:

```text
src/ai_dev_loop/integrations/codex/windows_home.py
```

Resolution order:

1. Explicit `--windows-codex-home`.
2. `CODEX_DESKTOP_HOME`.
3. Windows `%USERPROFILE%` via `cmd.exe /c echo %USERPROFILE%` plus `wslpath -u`.
4. Safe failure with an actionable error.

The path must be absolute and must end with `.codex`. Passing the Windows user profile instead of the Codex home is rejected.

## WSL Invocation

WSL invocation logic lives in:

```text
src/ai_dev_loop/integrations/codex/wsl_invocation.py
```

WSL distro resolution order:

1. Explicit `--wsl-distro`.
2. `AI_DEV_LOOP_WSL_DISTRO`.
3. Best-effort `wsl.exe -l -v`.
4. Safe failure with an actionable error.

Desktop hook command generation now validates and quotes dynamic tokens:

- WSL distro names;
- Python command;
- WSL hook script path.

Rejected characters include:

```text
newlines, quotes, &, |, <, >, ^, %, !, (, )
```

Tokens containing spaces are quoted for the command string stored in Windows `hooks.json`.

## Desktop Session Bridge

Bridge implementation lives in:

```text
src/ai_dev_loop/integrations/codex/desktop_bridge.py
```

Bridge paths:

```text
WSL Codex home:
${CODEX_HOME:-$HOME/.codex}

WSL sessions directory:
<wsl-codex-home>/sessions

Bridge link:
<wsl-codex-home>/sessions/from-desktop

Desktop sessions target:
<windows-codex-home>/sessions
```

`integrations sessions install`:

- resolves WSL Codex home;
- rejects WSL `CODEX_HOME` under `/mnt/*`;
- rejects when the whole WSL `sessions/` directory is itself a symlink;
- creates the WSL sessions directory if missing;
- resolves and validates the Windows desktop sessions directory;
- creates or updates only the nested `from-desktop` symlink;
- refuses to replace a non-symlink path at `from-desktop`;
- reports reachable rollout count;
- does not touch SQLite files.

`integrations sessions status` reports:

- WSL Codex home;
- WSL sessions directory;
- bridge link path;
- bridge presence;
- bridge target;
- whether the target matches the resolved Windows desktop sessions directory;
- desktop sessions path;
- reachable rollout count;
- warnings.

`integrations sessions list`:

- requires the bridge to exist unless an explicit desktop sessions path is supplied;
- refuses a mispointed bridge;
- lists recent `rollout-*.jsonl` files;
- extracts session IDs from filenames only;
- does not read rollout contents.

`integrations sessions remove`:

- removes only the `from-desktop` symlink;
- refuses non-symlink files/directories at that path;
- preserves Windows session files;
- does not require Windows home detection in order to remove the WSL-side symlink.

## Install And Uninstall Behavior

`install_integrations()` is target-aware.

For `codex-desktop-wsl`, it:

1. Resolves Windows Codex home.
2. Resolves WSL distro.
3. Installs or updates the Windows-visible skill.
4. Installs or updates the WSL hook script.
5. Generates a `wsl.exe` hook command.
6. Merges that command into the Windows `hooks.json`.
7. Optionally installs the session bridge only when `--install-session-bridge` is supplied.
8. Otherwise reports bridge status and next action.
9. Prints a desktop-specific trust instruction.

`uninstall_integrations()` is also target-aware.

For `codex-desktop-wsl`, it:

- removes only the Windows-home skill and Windows hook registration for the desktop target;
- preserves the WSL hook script;
- preserves the session bridge;
- preserves unrelated hooks and unrelated Windows-home state.

For `wsl-cli`, uninstall preserves the Phase 7 behavior and removes the WSL skill, WSL hook script, and WSL hook registration.

## Doctor And Status

`integrations status` now emits schema version `2` and includes:

- selected target;
- WSL home;
- skill home;
- Codex home;
- Windows Codex home when applicable;
- installed paths;
- package-content match booleans;
- hook registration command;
- trust status;
- remediation items;
- session bridge payload for the desktop target when resolvable.

Hook trust remains `unknown`.

`doctor` now includes `wsl-cli` integration checks and includes `codex-desktop-wsl` checks when a Windows Codex home is detectable or explicitly supplied. Desktop bridge checks are read-only.

## Skill Recovery Guidance

The package-owned handoff skill was updated so missing SessionStart context recovery mentions Codex Desktop explicitly.

For Codex Desktop on Windows with WSL agents, it now tells Codex to:

- run `ai_dev_loop integrations status --target codex-desktop-wsl`;
- trust the hook in Codex Desktop `/hooks`;
- run `ai_dev_loop integrations sessions status`;
- use `ai_dev_loop integrations sessions list` only as a manual recovery path to discover desktop session IDs;
- pass `--codex-session-id` only when the exact ID is known from a trusted source;
- never use `--last`.

## Review Findings Fixed During Phase 7.5

Several staged-review findings were valid and fixed. Future agents should keep these behaviors covered.

### P2: Mispointed desktop bridge symlink reported as healthy

Initial code treated any `from-desktop` symlink as a healthy bridge.

Risk:

- `status` and `doctor` could report a bridge as present even when it pointed somewhere other than the resolved Windows desktop sessions directory.
- `codex exec resume <desktop-session-id>` could fail because the intended rollout files were not visible.

Fix:

- Added `bridge_target_matches: bool | None` to `DesktopBridgeStatus`.
- `collect_bridge_status()` resolves the symlink and compares it to the resolved Windows desktop sessions directory.
- Mispointed symlinks add a warning, set `bridge_target_matches=False`, and skip rollout counting.
- `doctor` treats `bridge_target_matches is False` as unhealthy.
- `list` refuses a mispointed bridge.
- Added `test_mispointed_symlink_reports_warning`.

### P2: Bridge removal depended on Windows home detection

Initial `remove_desktop_bridge()` used full bridge-path resolution, which required Windows Codex home detection even though removal only needs the WSL symlink path.

Risk:

- A user could be unable to remove a stale bridge if Windows home autodetection later failed.

Fix:

- Added `resolve_wsl_bridge_paths()` for WSL-only path resolution.
- Added `try_resolve_desktop_sessions_dir()` for optional Windows diagnostics.
- `remove_desktop_bridge()` now uses only WSL paths.
- `collect_bridge_status()` reports Windows detection failures as warnings when possible.
- Added `test_remove_works_when_windows_home_detection_fails`.

### P3: WSL distro names with spaces broke desktop hook command

Initial desktop hook command generation interpolated distro names directly.

Risk:

- Distro names such as `Ubuntu Preview` produced invalid hook commands.

Fix:

- Distro names are validated.
- Tokens containing spaces are quoted.
- Unsafe distro characters are rejected.
- Added `test_build_desktop_hook_command_quotes_distro_with_spaces`.
- Added `test_build_desktop_hook_command_rejects_unsafe_distro_chars`.

### P2: Desktop bridge tests were not hermetic under WSL

Initial tests used `tmp_path` and inherited `CODEX_HOME` directly.

Risk:

- On this WSL workstation, Windows-propagated `TMP` or `CODEX_HOME` can point under `/mnt/c`.
- The product correctly rejects WSL `CODEX_HOME` under `/mnt/*`, causing tests to fail for environmental reasons.

Fix:

- Added hermetic native-WSL fixtures in `tests/conftest.py`:
  - `hermetic_tmp_path`;
  - `hermetic_home`;
  - `hermetic_xdg`;
  - `hermetic_codex_env`;
  - `propagated_windows_env`;
  - `hermetic_desktop_cli_env`.
- Updated desktop bridge and desktop integration tests to use hermetic paths.
- CLI session tests pass `--wsl-codex-home` explicitly.
- Added `test_sessions_install_cli_with_propagated_windows_env`.

### P2: Legacy shell bridge was staged as product interface

The earlier staged set included:

```text
scripts/codex-desktop-wsl-sessions.md
scripts/share-codex-desktop-sessions.sh
```

Risk:

- The shell script duplicated the new typed Python session commands.
- Its previous `ln -sfn` behavior was weaker than the Python implementation because it could replace an existing non-symlink bridge path.
- The markdown promoted the shell script as setup path, conflicting with the Phase 7.5 product surface.

Fix:

- Both files were removed from the staged set.
- They remain untracked reference material.
- Phase 8 can fold the markdown into final docs.

### P3: Dynamic desktop hook command tokens were not all validated

Initial code validated/quoted distro and script path partially, but interpolated `python_command` raw.

Risk:

- A Python executable path with spaces could break the hook command.
- Windows command metacharacters in dynamic tokens could produce invalid or altered command text.

Fix:

- Added shared command-token validation.
- `python_command` is validated and quoted.
- hook script path is validated and quoted.
- Windows command metacharacters are rejected.
- Added tests for Python path spaces, Python metacharacters, and hook path metacharacters.

## Tests And Validation

The final reported suite after fixes:

```text
255 passed
```

Validation performed during review:

```bash
git diff --cached --check
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
```

Observed results:

```text
git diff --cached --check: passed
ruff format --check: 75 files already formatted
ruff check: All checks passed
mypy src: Success, no issues found in 47 source files
pytest with TMP=/tmp: 255 passed
python -m build: successfully built sdist and wheel
```

Additional targeted validation was run with Windows-propagated DrvFS environment values:

```bash
TMPDIR=/mnt/c/Users/RODRIG~1/AppData/Local/Temp \
TMP=/mnt/c/Users/RODRIG~1/AppData/Local/Temp \
TEMP=/mnt/c/Users/RODRIG~1/AppData/Local/Temp \
CODEX_HOME=/mnt/c/Users/RODRIG~1/.codex \
uv run python -m pytest -q -s \
  tests/unit/test_codex_desktop_bridge.py \
  tests/unit/test_codex_integration_targets.py \
  tests/integration/test_desktop_integrations.py
```

Observed result:

```text
36 passed
```

Validation caveat:

- `uv run python -m pytest -q` with pytest capture enabled and process-level `TMP` inherited from DrvFS failed before test collection due to a pytest capture temp-file `FileNotFoundError`.
- This happens before fixtures can run.
- The plan's validation command uses `-s`, and the targeted DrvFS run with `-s` passed.
- The full suite passed with native temp values (`TMPDIR=/tmp TMP=/tmp TEMP=/tmp`).

Future agents should prefer native WSL temp for full-suite validation or use `-s` when explicitly simulating DrvFS temp propagation.

## Real Workstation State

No real Phase 7.5 install into the user's Windows Codex home was performed during this implementation/review cycle.

The user's manual desktop session bridge already existed before Phase 7.5:

```text
/home/rojobad/.codex/sessions/from-desktop -> /mnt/c/Users/Rodrigo Badia/.codex/sessions
```

Cursor's implementation summary reported this bridge as present and desktop rollouts reachable. It also reported that Windows skill/hooks were not yet installed for the desktop target.

Recommended next real workstation command, only when the user approves real installation:

```bash
uv run ai_dev_loop integrations install --target codex-desktop-wsl
```

If the existing manual bridge should be preserved and no bridge mutation is desired, do not pass `--install-session-bridge`.

After install, the user must open Codex Desktop `/hooks` and trust the `ai_dev_loop` hook there.

Useful status commands before/after real install:

```bash
uv run ai_dev_loop integrations status --target codex-desktop-wsl
uv run ai_dev_loop integrations sessions status
uv run ai_dev_loop integrations sessions list
```

If WSL distro detection fails or is ambiguous, pass:

```bash
--wsl-distro <distro>
```

If Windows home detection fails, pass:

```bash
--windows-codex-home "/mnt/c/Users/<windows-user>/.codex"
```

## Privacy And Safety

Phase 7.5 preserves the privacy posture from earlier phases:

- no transcript contents are read or copied;
- desktop rollout listing reads filenames only;
- no Codex SQLite databases are shared, copied, opened, migrated, or inspected;
- no auth files or tokens are copied;
- hook trust remains manual and unknown from local files;
- default output does not print prompts, patches, raw JSONL, or transcript contents;
- session bridge commands touch only `sessions/from-desktop`;
- bridge removal refuses non-symlink paths.

The WSL hook script remains standalone and standard-library-only.

## Important Invariants For Future Agents

- Keep `wsl-cli` backward compatible.
- Keep `codex-desktop-wsl` explicit; do not silently switch targets.
- Do not assume Codex Desktop can see WSL `~/.codex/hooks.json`.
- Do not assume WSL CLI can see Windows desktop sessions without the nested bridge.
- Do not set WSL `CODEX_HOME` to `/mnt/c/.../.codex`.
- Do not share whole `.codex` homes or SQLite state across Windows and WSL.
- Do not symlink the whole WSL `sessions/` directory to Windows.
- Do not make `integrations install --target codex-desktop-wsl` create the bridge unless an explicit flag is supplied.
- Do not make `integrations uninstall --target codex-desktop-wsl` remove the bridge by default.
- Do not remove the WSL hook script during desktop uninstall.
- Keep bridge removal WSL-only so it works even when Windows home detection fails.
- Keep bridge status validating that `from-desktop` points at the resolved Windows sessions directory.
- Keep `integrations sessions list` reading filenames only, not rollout contents.
- Keep desktop hook command generation validating and quoting dynamic tokens.
- Keep hook trust status `unknown`; do not inspect undocumented Codex trust state.
- Keep the handoff skill recovery guidance explicit about Codex Desktop `/hooks`.
- Never use `--last` for handoff or review recovery.
- Do not infer, shorten, guess, or substitute Codex session IDs.

## Current Repository Notes

At the time this handoff was written, the staged Phase 7.5 set included:

```text
.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc
README.md
plans/phase-7-5-codex-desktop-wsl-bridge.md
plans/prompt_phase-7-5-codex-desktop-wsl-bridge.txt
src/ai_dev_loop/cli.py
src/ai_dev_loop/commands/doctor.py
src/ai_dev_loop/commands/integrations.py
src/ai_dev_loop/integrations/codex/desktop_bridge.py
src/ai_dev_loop/integrations/codex/hooks_json.py
src/ai_dev_loop/integrations/codex/install.py
src/ai_dev_loop/integrations/codex/paths.py
src/ai_dev_loop/integrations/codex/skill/SKILL.md
src/ai_dev_loop/integrations/codex/target.py
src/ai_dev_loop/integrations/codex/windows_home.py
src/ai_dev_loop/integrations/codex/wsl_invocation.py
tests/conftest.py
tests/integration/test_desktop_integrations.py
tests/integration/test_integrations.py
tests/unit/test_codex_desktop_bridge.py
tests/unit/test_codex_integration_targets.py
```

The following files were intentionally left untracked as reference material for Phase 8 documentation cleanup:

```text
scripts/codex-desktop-wsl-sessions.md
scripts/share-codex-desktop-sessions.sh
```

This findings file itself should be staged separately if the user wants it included with the Phase 7.5 handoff.

## Known Pending Work

Phase 8 should focus on final acceptance hardening:

- decide whether and how to fold `scripts/codex-desktop-wsl-sessions.md` into official docs;
- keep the shell helper untracked, delete it, or convert it into a thin wrapper around the Python CLI after explicit approval;
- run disposable end-to-end acceptance tests;
- verify package install outside editable/dev mode;
- perform real workstation `codex-desktop-wsl` installation only after explicit user approval;
- verify Codex Desktop hook trust manually in Desktop `/hooks`;
- verify `ai_dev_loop integrations sessions list` can discover desktop rollout session IDs;
- avoid model activity unless explicitly approved.

## Open Issues

No actionable staged-review findings remained after the final Phase 7.5 review.

Residual validation caveat:

- pytest capture can fail before collection when the process-level temp directory is DrvFS. Use native WSL temp for full-suite validation or run relevant DrvFS simulations with `-s`.
