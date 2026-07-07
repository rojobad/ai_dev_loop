# Phase 7.5 Plan: Codex Desktop WSL Bridge

## Goal or Goals

Implement explicit support for the user's real Codex topology:

- Codex Desktop runs on Windows.
- Cursor and Codex CLI subprocesses run in WSL.
- Target repositories live in WSL.
- Codex Desktop and Codex CLI use different `.codex` homes.

After Phase 7.5, `ai_dev_loop` must support two Codex integration targets:

```text
wsl-cli
codex-desktop-wsl
```

The existing Phase 7 behavior becomes the `wsl-cli` target. The new `codex-desktop-wsl` target installs Codex-visible assets into the Windows user's Codex home, while executing the hook through WSL and writing `ai_dev_loop` session metadata under WSL XDG state.

This phase must also productize the desktop-session rollout bridge that is currently represented by the untracked helper script:

```text
scripts/share-codex-desktop-sessions.sh
```

The official product surface should be `ai_dev_loop integrations sessions ...`, not a loose shell script. The shell script may remain untracked or be kept temporarily as reference material, but the implementation should live in typed Python.

## Non-Goals

- Do not run real Cursor or Codex model activity.
- Do not run real `ai_dev_loop start` against a live desktop session.
- Do not automatically bypass or mutate Codex hook trust state.
- Do not share the entire Windows `.codex` home with WSL.
- Do not set WSL `CODEX_HOME` to `/mnt/c/.../.codex`.
- Do not symlink, copy, migrate, open, or inspect Codex SQLite databases across Windows and WSL.
- Do not symlink the entire WSL `~/.codex/sessions` directory to Windows.
- Do not create commits, pushes, resets, cleans, stashes, unstaging, rollbacks, or destructive cleanup.
- Do not delete the untracked `scripts/codex-desktop-wsl-sessions.md` documentation draft.
- Do not treat the untracked shell script as the long-term product interface.
- Do not make `integrations uninstall` remove the desktop session bridge unless the user explicitly requests bridge removal.
- Do not make `integrations install --target codex-desktop-wsl` silently create a `/mnt/c` session symlink unless an explicit flag is supplied.

## Scope

Implement Phase 7.5 as defined by:

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `phase-1-findings.md`
- `phase-2-findings.md`
- `phase-3-findings.md`
- `phase-4-findings.md`
- `phase-5-findings.md`
- `phase-6-findings.md`
- `phase-7-findings.md`
- `plans/phase-6-real-abort.md`
- `plans/phase-7-global-codex-integrations.md`
- `scripts/codex-desktop-wsl-sessions.md`
- `scripts/share-codex-desktop-sessions.sh`
- this plan.

This phase owns:

1. Target-aware Codex integration install/status/uninstall.
2. A new `codex-desktop-wsl` target.
3. Windows Codex home detection and explicit override handling.
4. WSL distribution detection and explicit override handling.
5. WSL hook command generation for Codex Desktop hooks.
6. Desktop session bridge logic migrated from shell to Python.
7. `ai_dev_loop integrations sessions install/status/list/remove`.
8. Read-only doctor/status diagnostics for the desktop bridge.
9. Tests that prove desktop integration writes to the Windows Codex-visible home but WSL session metadata remains under WSL XDG state.
10. Tests that prove only rollout session files are bridged, never SQLite state.
11. README and governance updates describing the two targets and the safe bridge.

## Out of Scope

- Do not finish Phase 8 validation/documentation polish except where needed to explain this bridge.
- Do not require paid model calls or real Codex/Cursor model activity.
- Do not require the user to trust the hook during automated tests.
- Do not detect hook trust by reading private or undocumented Codex trust state.
- Do not implement broad migration or repair for corrupted Windows or WSL Codex homes.
- Do not move existing WSL-only installed assets unless the user requests uninstall.
- Do not delete `scripts/codex-desktop-wsl-sessions.md`; Phase 8 can fold it into final docs.
- Do not delete `scripts/share-codex-desktop-sessions.sh` unless a later explicit cleanup plan approves that.

## Required Context

Read these files before implementing:

- `plan-2-build-ai-dev-loop-orchestrator.md`
- `phase-0-findings.md`
- `phase-1-findings.md`
- `phase-2-findings.md`
- `phase-3-findings.md`
- `phase-4-findings.md`
- `phase-5-findings.md`
- `phase-6-findings.md`
- `phase-7-findings.md`
- `plans/phase-6-real-abort.md`
- `plans/phase-7-global-codex-integrations.md`
- `plans/prompt_phase-7-global-codex-integrations.txt`
- `scripts/codex-desktop-wsl-sessions.md`
- `scripts/share-codex-desktop-sessions.sh`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`

Important current implementation facts:

- Phase 7 installed WSL-only global assets into the user's real WSL home:
  - `/home/rojobad/.agents/skills/ai-dev-loop-handoff/SKILL.md`
  - `/home/rojobad/.codex/hooks/ai_dev_loop_session_start.py`
  - `/home/rojobad/.codex/hooks.json`
- Those WSL assets are not visible to Codex Desktop on Windows.
- Codex Desktop reads the Windows profile home, for example:

  ```text
  /mnt/c/Users/Rodrigo Badia/.codex
  ```

- `codex exec resume <session-id>` from WSL can resume desktop-originated sessions only when WSL can see the desktop rollout files by session ID.
- The working manual bridge is a nested symlink:

  ```text
  /home/<wsl-user>/.codex/sessions/from-desktop
    -> /mnt/c/Users/<windows-user>/.codex/sessions
  ```

- The interactive `codex resume` picker does not list desktop sessions because they are not indexed in the WSL CLI state database.
- `prepare --codex-session-id` remains the safe manual fallback when SessionStart context is unavailable.

## Cursor Rules And Skills

Follow these repo-local governance inputs:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`

No `AGENTS.md` file is present.

If this phase changes global integration contracts materially, update `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc` so future agents preserve the desktop bridge invariants.

## Architecture Guardrails

- `ai_dev_loop` remains a deterministic local orchestrator.
- The original Codex session remains the only authoritative reviewer.
- Every review still uses `codex exec resume <exact-session-id>` from WSL.
- Never use `--last`.
- Never infer, shorten, guess, or substitute Codex session IDs.
- The orchestrator must not parse review Markdown to make loop decisions.
- Do not change bounded loop, resume, abort, Cursor chat identity, or Codex review semantics unless strictly required by target-aware integration plumbing.
- All subprocess calls must use argument arrays with `shell=False`.
- Global integration install must preserve unrelated hooks, skills, and user files.
- Hook trust remains manual and unknown from local files.
- For `codex-desktop-wsl`, install Codex-visible assets into the Windows Codex-visible home.
- For `codex-desktop-wsl`, the hook command must invoke WSL explicitly.
- The WSL hook script must remain standalone and standard-library-only.
- The WSL hook must write session metadata under WSL XDG state, not Windows application state.
- Store only transcript path metadata supplied by the hook. Never read transcript contents.
- Do not store auth files, API keys, tokens, full process environments, prompts, patches, review contents, or raw JSONL in default output.
- Apply user-only permissions where supported.
- Fail safely on ambiguous Windows home, WSL distro, symlink, hook JSON, or path state.

## Design Requirements

### Integration Targets

Add a typed target concept, for example:

```python
class CodexIntegrationTarget(StrEnum):
    WSL_CLI = "wsl-cli"
    CODEX_DESKTOP_WSL = "codex-desktop-wsl"
```

Behavior:

- Existing install/status/uninstall behavior maps to `wsl-cli`.
- CLI defaults may remain `wsl-cli` for backward compatibility, but user-facing docs should recommend passing `--target` explicitly.
- `codex-desktop-wsl` must resolve separate WSL and Windows homes.
- JSON output must include the selected target.
- Human output must include enough path detail that the user can see which home is being managed.

### CLI Surface

Extend commands equivalent to:

```text
ai_dev_loop integrations install --target wsl-cli
ai_dev_loop integrations install --target codex-desktop-wsl
ai_dev_loop integrations uninstall --target wsl-cli
ai_dev_loop integrations uninstall --target codex-desktop-wsl
ai_dev_loop integrations status --target wsl-cli
ai_dev_loop integrations status --target codex-desktop-wsl
```

Add explicit desktop options where useful:

```text
--windows-codex-home
--wsl-distro
--wsl-hook-python
--wsl-hook-script-path
```

Names may be adapted to local CLI style, but responsibilities must remain clear.

Add a session bridge command group:

```text
ai_dev_loop integrations sessions install
ai_dev_loop integrations sessions status
ai_dev_loop integrations sessions list
ai_dev_loop integrations sessions remove
```

For machine output, support `--output json` where relevant.

### Conservative Bridge Creation

Do not make `ai_dev_loop integrations install --target codex-desktop-wsl` silently create the session symlink by default.

Recommended behavior:

1. Install or update the Windows-home skill.
2. Install or update the WSL hook script.
3. Merge the Windows-home hook registration.
4. Check desktop session bridge status.
5. If missing, print a clear next command:

   ```text
   ai_dev_loop integrations sessions install
   ```

6. Optionally support an explicit flag such as:

   ```text
   --install-session-bridge
   ```

   If that flag is implemented, it must have tests and must call the same safe bridge logic as `integrations sessions install`.

### Windows Codex Home Resolution

Implement a resolver for the Windows Codex home.

Sources, in precedence order:

1. Explicit CLI flag such as `--windows-codex-home`.
2. Environment variable such as `CODEX_DESKTOP_HOME`.
3. Windows `%USERPROFILE%` via `cmd.exe /c echo %USERPROFILE%` plus `wslpath -u`, when available.
4. Safe failure with an actionable message.

Validation:

- The resolved path must be absolute.
- The resolved path should end in `.codex`, or the CLI should clearly document whether the flag expects the Codex home or Windows user profile.
- If the Windows `sessions/` directory is required, verify it exists before creating the bridge.
- Do not create or modify Windows SQLite databases.
- Do not fail merely because `.codex/hooks.json` does not exist; first-time install must create it safely.

Tests must use fixture paths and must not require real `cmd.exe`, `wslpath`, or `/mnt/c`.

### WSL Identity And Hook Command

Implement a resolver for WSL invocation details.

Sources, in precedence order:

1. Explicit CLI flag such as `--wsl-distro`.
2. Environment variable such as `AI_DEV_LOOP_WSL_DISTRO`.
3. Best-effort detection using safe local commands only when available.
4. Safe failure with an actionable message if required and ambiguous.

The generated desktop hook command must be equivalent to:

```text
wsl.exe -d <distro-name> --exec python3 /home/<wsl-user>/.codex/hooks/ai_dev_loop_session_start.py
```

Requirements:

- Generate the command as a string because `hooks.json` stores command text, but never execute it through `shell=True` inside `ai_dev_loop`.
- Quote/escape paths only in a way Codex Desktop can execute. Prefer avoiding spaces in the WSL-side hook path.
- Validate that the WSL hook script path exists in the WSL filesystem.
- The WSL hook script must be installed under the WSL home, not copied to Windows.
- The hook command in Windows `hooks.json` must point to the WSL hook script through `wsl.exe`.
- Status JSON must expose the registered command.

### Target-Aware Installed Assets

For `wsl-cli`:

```text
WSL skill:
/home/<wsl-user>/.agents/skills/ai-dev-loop-handoff/SKILL.md

WSL hook:
/home/<wsl-user>/.codex/hooks/ai_dev_loop_session_start.py

WSL hooks JSON:
/home/<wsl-user>/.codex/hooks.json
```

For `codex-desktop-wsl`:

```text
Windows-visible skill:
/mnt/c/Users/<windows-user>/.agents/skills/ai-dev-loop-handoff/SKILL.md

WSL hook script:
/home/<wsl-user>/.codex/hooks/ai_dev_loop_session_start.py

Windows-visible hooks JSON:
/mnt/c/Users/<windows-user>/.codex/hooks.json
```

The Windows `hooks.json` command must invoke the WSL hook script via `wsl.exe`.

Uninstall behavior:

- `uninstall --target wsl-cli` removes only WSL target assets and WSL hook registration.
- `uninstall --target codex-desktop-wsl` removes only Windows-home skill and Windows hook registration for the desktop target.
- Preserve the WSL hook script if it is still needed by `wsl-cli` or by another target unless ownership can be safely determined.
- Preserve unrelated hooks, unrelated skills, run history, XDG state, and the desktop session bridge by default.
- If removing the WSL hook script would break another target, report it and leave it in place.

### Desktop Session Bridge

Migrate the safety properties of `scripts/share-codex-desktop-sessions.sh` into Python.

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

`integrations sessions install` must:

1. Resolve WSL Codex home.
2. Reject WSL `CODEX_HOME` values under `/mnt/*`.
3. Reject when the whole WSL sessions directory is itself a symlink.
4. Create the WSL sessions directory if missing.
5. Resolve and validate the Windows desktop sessions directory.
6. Create or update only the nested `from-desktop` symlink.
7. Report the number of reachable rollout files when cheap.
8. Avoid touching any SQLite files.

`integrations sessions status` must report:

- WSL Codex home;
- WSL sessions directory;
- bridge link path;
- link presence;
- link target;
- desktop sessions path;
- reachable rollout count when cheap;
- warnings for unsafe states.

`integrations sessions list` must:

- require the bridge to exist or accept an explicit desktop sessions path;
- list recent `rollout-*.jsonl` files;
- extract and display session IDs where filenames match known rollout naming;
- avoid reading full rollout contents.

`integrations sessions remove` must:

- remove only the `from-desktop` symlink;
- refuse to remove non-symlink files/directories at that path unless an explicit later plan adds a safer repair command;
- preserve Windows session files.

### Hook Session Metadata

The same installed WSL hook script can serve both targets.

When Codex Desktop invokes it through `wsl.exe`, the hook may receive Windows-style paths in `cwd` or `transcript_path`. The hook must:

- store them as strings only;
- not read them;
- not require them to exist in WSL;
- still validate `session_id` strictly before using it in filenames.

Session records remain under:

```text
$XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
```

or the documented fallback:

```text
~/.local/state/ai_dev_loop/codex-sessions/<session-id>.json
```

### Handoff Skill Recovery Text

Update the package-owned handoff skill if needed.

It should still prefer SessionStart context, but when context is absent it should mention:

- for Codex Desktop on Windows, verify `ai_dev_loop integrations status --target codex-desktop-wsl`;
- trust the hook in Codex Desktop `/hooks`;
- ensure `ai_dev_loop integrations sessions status` is healthy;
- use `ai_dev_loop integrations sessions list` to discover desktop session IDs only as a manual recovery path;
- pass `--codex-session-id` manually only when the exact ID is known from a trusted source.

Do not tell Codex to use `--last`.

### Doctor And Status

Update `doctor` to include read-only checks for:

- `wsl-cli` integration status;
- `codex-desktop-wsl` integration status when requested or when a Windows Codex home is detectable;
- WSL hook script presence;
- Windows-home hook registration;
- desktop session bridge status;
- hook trust status as `unknown`.

`doctor` must not modify configuration or install assets unless a future explicit repair flag is added.

### Documentation

Update README enough for Phase 7.5:

- explain the two targets;
- explain why WSL and Windows Codex homes differ;
- state that sharing whole `CODEX_HOME` is forbidden;
- show the safe session bridge;
- document `integrations sessions ...`;
- explain that Codex Desktop hook trust happens in Codex Desktop `/hooks`;
- explain the manual fallback using `--codex-session-id`.

Do not fully fold `scripts/codex-desktop-wsl-sessions.md` into final docs in this phase if that is better left to Phase 8, but make README accurate enough that users do not follow the old WSL-only instructions by mistake.

## Implementation Plan

1. Read required context and inspect current Phase 7 integration implementation.
2. Add target-aware path/config models for Codex integration.
3. Refactor existing install/status/uninstall to operate on a target context.
4. Preserve `wsl-cli` behavior and tests.
5. Implement Windows Codex home resolver with explicit override and testable detection hooks.
6. Implement WSL distro/hook command resolver with explicit override and testable command generation.
7. Implement desktop target install/status/uninstall:
   - Windows skill;
   - WSL hook script;
   - Windows hooks JSON registration using `wsl.exe`;
   - no implicit session symlink creation by default.
8. Implement desktop session bridge module, for example `src/ai_dev_loop/integrations/codex/desktop_bridge.py`.
9. Wire `integrations sessions install/status/list/remove` through Typer.
10. Update `integrations status` JSON/text to include target and desktop diagnostics.
11. Update `doctor` with read-only bridge checks.
12. Update handoff skill recovery guidance if needed.
13. Update README and global integration governance rule.
14. Add unit tests for resolvers, command generation, bridge logic, unsafe path rejection, and hook JSON target behavior.
15. Add integration tests for CLI install/status/uninstall and sessions commands using temporary WSL and Windows-style fixture homes.
16. Run full validation.
17. Do not run real install into the user's Windows home unless the user explicitly approves it after validation.

## Testing Requirements

Add or update tests for:

- existing `wsl-cli` first-time install still passes;
- existing `wsl-cli` idempotent install still passes;
- existing `wsl-cli` uninstall preservation tests still pass;
- `install --target codex-desktop-wsl` writes skill under the Windows fixture home;
- `install --target codex-desktop-wsl` writes hook registration under the Windows fixture home;
- desktop hook command uses `wsl.exe`, the selected distro, `--exec`, and the WSL hook script path;
- desktop install does not create `sessions/from-desktop` unless explicit bridge install is requested;
- `integrations sessions install` creates only the nested symlink;
- `integrations sessions install` rejects WSL `CODEX_HOME` under `/mnt/*`;
- `integrations sessions install` rejects a symlinked whole WSL sessions directory;
- `integrations sessions status --output json` reports bridge paths and rollout counts;
- `integrations sessions list` lists session IDs from rollout filenames without reading contents;
- `integrations sessions remove` removes only the symlink;
- `integrations sessions remove` refuses non-symlink bridge paths;
- desktop uninstall preserves unrelated Windows hooks;
- desktop uninstall preserves the session bridge by default;
- invalid Windows `hooks.json` fails safely without deleting installed files;
- package build still includes skill and hook assets;
- doctor JSON includes desktop target checks without mutating files.

Automated tests must use temporary `HOME`, XDG directories, fake Windows homes, and monkeypatched command detection. They must not depend on the real `/mnt/c`, `cmd.exe`, `wslpath`, or `wsl.exe` being available.

## Validation Commands

Run at least:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s
uv run python -m build
uv run ai_dev_loop --help
uv run ai_dev_loop integrations status
uv run ai_dev_loop integrations status --target codex-desktop-wsl
uv run ai_dev_loop integrations sessions status
uv run ai_dev_loop doctor
```

If a command requires real Windows/WSL state and cannot run in automated fixtures, provide a safe explanation and ensure the equivalent fixture-backed tests pass.

## Acceptance Criteria

- `wsl-cli` remains backward compatible with Phase 7 behavior.
- `codex-desktop-wsl` target is implemented and documented.
- Desktop target installs Codex-visible skill and hooks JSON into the Windows Codex-visible home.
- Desktop target hook command invokes WSL explicitly and points at the WSL hook script.
- The WSL hook script writes session metadata under WSL XDG state.
- `integrations install --target codex-desktop-wsl` does not silently create the desktop session symlink by default.
- `integrations sessions install/status/list/remove` safely manages the desktop rollout bridge.
- The bridge exposes only desktop session rollout files by nested symlink.
- The implementation never shares or migrates Codex SQLite state across Windows and WSL.
- Uninstall preserves unrelated hooks, skills, run history, XDG state, and the session bridge by default.
- README no longer implies that WSL-only hook installation is sufficient for Codex Desktop.
- Unit and integration tests cover the new bridge behavior.
- Full validation passes.

## Manual Verification After Implementation

Do not perform these steps unless the user explicitly approves real workstation changes after automated validation.

Recommended manual checks after approval:

```bash
wsl.exe -l -v
uv run ai_dev_loop integrations status --target codex-desktop-wsl
uv run ai_dev_loop integrations install --target codex-desktop-wsl --wsl-distro <distro>
uv run ai_dev_loop integrations sessions status
uv run ai_dev_loop integrations sessions install
uv run ai_dev_loop integrations sessions list
codex exec resume <desktop-session-id> "Reply with a one-line dry-run acknowledgement."
```

The final `codex exec resume` sends a real prompt and should be run only if the user approves model activity. A safer non-model check is to use `integrations sessions list` plus a fake/fixture resume test.

## Phase 8 Boundary

Phase 8 should perform final acceptance hardening:

- disposable E2E acceptance tests;
- package install outside editable/dev mode;
- final README expansion;
- troubleshooting cleanup;
- any final documentation derived from `scripts/codex-desktop-wsl-sessions.md`;
- real workstation verification only with explicit user approval.

## Open Questions

None blocking. Use conservative defaults:

- default target may remain `wsl-cli`;
- `codex-desktop-wsl` should require explicit selection;
- session bridge creation should require explicit `integrations sessions install` or an explicit bridge flag;
- if Windows home or WSL distro detection is ambiguous, fail safely and ask for explicit flags.
