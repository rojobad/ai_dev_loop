# Phase 7 Plan: Global Codex Integrations

## Goal or Goals

Implement the global Codex integration layer for `ai_dev_loop`.

After Phase 7, a user must be able to run:

```bash
ai_dev_loop integrations install
```

on a WSL/Linux machine and get the local user-level Codex assets required for the approved-plan handoff workflow:

1. a global handoff skill at `$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md`;
2. a SessionStart hook script under `$HOME/.codex/hooks/ai_dev_loop_session_start.py`;
3. an idempotently merged hook registration in `$HOME/.codex/hooks.json`;
4. clear instructions to open `/hooks` in Codex and trust the new or changed hook definition;
5. `integrations status`, `doctor`, and uninstall behavior that accurately report and manage those assets.

The user has explicitly approved installing these global assets during this phase. Automated tests must still use temporary `HOME` and XDG directories by default. Only after implementation and validation should the real install command be run against the user's actual HOME.

## Non-Goals

- Do not change the bounded Cursor/Codex review-fix loop semantics from Phase 5.
- Do not change real abort semantics from Phase 6.
- Do not bypass Codex hook trust or modify Codex trust state directly.
- Do not install anything under deprecated or invented paths such as `~/.codex/skills`.
- Do not delete unrelated skills, hooks, hook groups, run history, config files, or state directories.
- Do not read, copy, summarize, or persist Codex transcript contents.
- Do not store authentication tokens, API keys, full process environments, full prompts, full patches, or raw review contents in default output.
- Do not invoke real Cursor or Codex model activity in automated tests.
- Do not add commits, pushes, resets, cleans, stashes, unstaging, rollbacks, or destructive cleanup.
- Do not implement broad repair/migration for corrupted user files beyond clear validation errors and safe backups.

## Scope

Implement Phase 7 as defined by:

- `plan-2-build-ai-dev-loop-orchestrator.md`;
- `phase-0-findings.md`;
- `phase-1-findings.md`;
- `phase-2-findings.md`;
- `phase-3-findings.md`;
- `phase-4-findings.md`;
- `phase-5-findings.md`;
- `phase-6-findings.md`;
- `plans/phase-6-real-abort.md`;
- this plan.

This phase owns:

1. A package-owned global handoff skill template.
2. A package-owned SessionStart hook script.
3. Hook input/output handling for SessionStart.
4. Safe hook JSON merge, backup, idempotency, validation, and uninstall.
5. Real `ai_dev_loop integrations install`.
6. Real `ai_dev_loop integrations uninstall`.
7. Improved `ai_dev_loop integrations status`.
8. Improved `ai_dev_loop doctor` checks for global skill, hook script, hook registration, and detectable trust status.
9. Tests for install/status/uninstall using temporary `HOME`.
10. README/troubleshooting updates for daily integration use.
11. Final real installation into this user's HOME after automated validation.

## Out of Scope

- Do not require target repositories to know where XDG state lives.
- Do not place run state, hook session records, prompts, reviews, or agent logs inside target repositories.
- Do not validate target-repository review skills by inspecting this repository's `.agents/skills`.
- Do not add a daemon or background service.
- Do not make `prepare` infer a Codex session ID when the hook did not provide one or the caller did not pass one.
- Do not automatically start a run from the active Codex TUI.
- Do not remove `plan-2-build-ai-dev-loop-orchestrator.md:Zone.Identifier` or unrelated Windows metadata if present.
- Do not make uninstall remove run history unless a separately approved explicit destructive option is implemented with strong confirmation.

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
- `plans/phase-6-real-abort.md`
- `plans/prompt_phase-6-real-abort.txt`
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

- Phase 5 implemented the bounded review-fix loop and real `resume`.
- Phase 6 implemented real `abort` and active process metadata.
- `src/ai_dev_loop/commands/integrations.py` still has placeholder install/uninstall behavior.
- `src/ai_dev_loop/cli.py` wires `integrations install`, `uninstall`, and `status`.
- `src/ai_dev_loop/commands/doctor.py` currently verifies basic runtime/schema/CLI presence but not the global integration assets.
- `src/ai_dev_loop/paths.py` owns XDG path handling and secure directory creation patterns.
- Existing tests use temporary XDG directories; Phase 7 tests must also isolate `HOME`.
- Phase 0 found that `~/.codex/hooks.json` and `~/.agents/skills` did not exist at that time, but Phase 7 must handle both first-time and already-existing installations.

## Cursor Rules And Skills

Follow these repo-local governance inputs:

- `.cursor/rules/ai-dev-loop-governance.mdc`: project-wide implementation guardrails.
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`: state, locks, runners, Git safety, process execution, and orchestration contracts.
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`: persisted state, schema, manifest, artifact, and recovery checkpoint contracts.
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`: Codex review execution, structured review result, event logging, privacy, and artifact contracts.
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`: bounded loop, correction prompt ownership, multi-iteration artifacts, correction staging safety, resume idempotency, privacy, and tests.
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`: abort requests, active process metadata, process-group termination, state preservation, stale-metadata safety, privacy, and tests.
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`: global handoff skill, SessionStart hook, hook JSON merge/uninstall, trust instructions, integration status, doctor checks, packaging, permissions, and tests. This rule is required and must be kept aligned with this phase.
- `.agents/skills/create-cursor-plan/SKILL.md`: planning convention used to create this plan and prompt.
- `.agents/skills/review-staged-changes/SKILL.md`: review format for reviewing staged changes in this `ai_dev_loop` repository.

No `AGENTS.md` file is present.

## Architecture Guardrails

- `ai_dev_loop` remains a deterministic local orchestrator.
- All subprocess calls must use argument arrays with `shell=False`.
- All state, session records, logs, prompts, reviews, staged patches, abort metadata, and agent artifacts remain under XDG-managed `ai_dev_loop` paths outside target repositories.
- Global integration assets may be installed only under the documented user-level Codex paths:
  - `$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md`;
  - `$HOME/.codex/hooks/ai_dev_loop_session_start.py`;
  - `$HOME/.codex/hooks.json`.
- Directories should be `0700` where supported.
- Files containing session IDs, transcript paths, prompt instructions, or hook metadata should be `0600` where supported.
- The hook may store transcript path metadata supplied by Codex, but must never read or copy transcript contents.
- The hook must add only minimal additional context and must never expose auth material.
- Hook registration must preserve unrelated hooks and write atomically.
- Uninstall must remove only assets installed by `ai_dev_loop` and preserve unrelated hooks.
- Do not automatically bypass hook trust. The install command must tell the user to open `/hooks` and trust the new or changed hook.
- The installed skill must tell Codex to prepare the run and then instruct the user to exit the active TUI and run the returned start command.
- The installed skill must not tell Codex to run `ai_dev_loop start` from the active TUI.
- Keep Cursor chat identity, Codex session identity, structured review JSON decisions, and abort safety unchanged.

## Design Requirements

### Package Assets

Add package-owned integration assets under a focused path, for example:

```text
src/ai_dev_loop/integrations/codex/
├── session_start.py
├── hook_template.json
└── skill/
    └── SKILL.md
```

Requirements:

- Include these files in the built wheel/sdist through package configuration.
- Install/copy from package resources, not from working-tree-relative paths.
- Keep content deterministic so `integrations status` can compare installed files with the package version.
- Do not require editable installs for integration assets to work.

### Global Handoff Skill

Install to:

```text
$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md
```

The frontmatter must be equivalent to:

```yaml
---
name: ai-dev-loop-handoff
description: Prepare an approved local Codex implementation plan for the external ai_dev_loop orchestrator. Use after a plan and its Cursor prompt are finalized, when the user wants the same Codex session to review Cursor's staged implementation automatically.
---
```

The skill body must instruct Codex to:

- ensure the implementation plan is final and approved;
- ensure the separate Cursor prompt file exists;
- read the target repository's `ai_dev_loop.yaml`;
- use the current session ID from SessionStart context;
- run `ai_dev_loop prepare`;
- pass the exact Cursor prompt through stdin;
- parse the prepare result;
- never start the loop from the active TUI;
- tell the user to exit Codex with `/exit`;
- tell the user to run the returned `ai_dev_loop start <run-id>` command in the shell;
- provide a recovery path when the hook context is absent, such as instructing the user to verify `ai_dev_loop integrations status`, trust the hook, restart/resume Codex, or pass `--codex-session-id` only when they have the exact session ID.

The skill must not contain project-specific assumptions such as a fixed project name, fixed plan path, or fixed Cursor model.

### SessionStart Hook Script

Install to:

```text
$HOME/.codex/hooks/ai_dev_loop_session_start.py
```

The hook script must:

1. Read exactly one JSON object from stdin.
2. Accept `hook_event_name == "SessionStart"` and SessionStart sources:
   - `startup`;
   - `resume`;
   - `clear`;
   - `compact`.
3. Extract, when present:
   - `session_id`;
   - `transcript_path`;
   - `cwd`;
   - `model`;
   - `source`.
4. Write a minimal session record under the XDG state directory:

   ```text
   $XDG_STATE_HOME/ai_dev_loop/codex-sessions/<session-id>.json
   ```

   or an equivalent documented XDG state subdirectory.

5. Store only:
   - schema version;
   - session ID;
   - model;
   - cwd;
   - transcript path string if supplied;
   - source;
   - timestamp.
6. Never read transcript contents.
7. Never copy transcript contents.
8. Never store auth material.
9. Return valid JSON containing `hookSpecificOutput.additionalContext`.
10. Include additional context equivalent to:

```text
ai_dev_loop integration is active.
Current Codex session ID: <session-id>
Current Codex model: <model>
Current session working directory: <cwd>
When preparing an approved ai_dev_loop run, pass this exact session ID. Do not infer another session and do not use --last.
```

If input is incomplete, invalid, or missing a usable session ID, the hook should exit successfully with safe minimal JSON and no destructive side effects. It should not raise tracebacks into Codex output.

The script should be runnable as a standalone Python file with standard-library dependencies only, because it will execute outside the package import context after installation.

### Hook Registration

Register the hook in:

```text
$HOME/.codex/hooks.json
```

The registered shape must be equivalent to:

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

Requirements:

- Preserve unrelated top-level keys.
- Preserve unrelated hook events.
- Preserve unrelated SessionStart entries.
- Merge idempotently.
- Avoid duplicate `ai_dev_loop` command hook entries.
- Validate existing `hooks.json` before modifying it.
- If existing JSON is invalid, fail with a clear message and do not overwrite it.
- Back up the original file before writing a modified version.
- Write atomically.
- Use restrictive file permissions where supported.
- Include an uninstall marker or deterministic command identity so uninstall can remove only the `ai_dev_loop` hook entry.
- Do not automatically trust the hook.
- Install output must tell the user to open `/hooks` in Codex and trust the hook.
- Install output must state that the hook may be skipped until trusted.

### Integration Commands

Implement:

```bash
ai_dev_loop integrations install
```

It must:

- create app XDG directories;
- create `$HOME/.agents/skills/ai-dev-loop-handoff`;
- install/update the skill file;
- create `$HOME/.codex/hooks`;
- install/update the hook script;
- merge `$HOME/.codex/hooks.json`;
- back up an existing hooks file before modifying it;
- be idempotent;
- print installed paths;
- print whether each asset was created, updated, or already current;
- print the hook trust instructions;
- support `--output json` if practical and consistent with existing CLI conventions.

Implement:

```bash
ai_dev_loop integrations uninstall
```

It must:

- remove only the `ai-dev-loop-handoff` skill directory or files installed by `ai_dev_loop`;
- remove only the installed hook script if it matches or is recognized as the `ai_dev_loop` script;
- remove only the `ai_dev_loop` hook registration from `hooks.json`;
- preserve unrelated hooks and unrelated skills;
- back up `hooks.json` before modifying it;
- preserve run history and XDG state by default;
- not delete run directories, prompts, reviews, or logs;
- support `--output json` if practical;
- optionally add a destructive state-cleanup flag only if it is carefully confirmed and tested. It is acceptable to defer destructive state cleanup if README documents manual cleanup.

Implement/improve:

```bash
ai_dev_loop integrations status
```

It must report:

- skill path;
- skill installed;
- skill matches current package content;
- hook script path;
- hook script installed;
- hook script matches current package content;
- hooks.json path;
- hooks.json exists;
- hook registration present;
- duplicate registrations, if any;
- whether `hooks.json` parses cleanly;
- whether hook trust is detectable. If trust status cannot be detected safely, report `unknown` with an actionable explanation.

### Doctor Integration Checks

Update `ai_dev_loop doctor` to include:

- global handoff skill installation status;
- hook script installation status;
- hook registration status;
- hooks.json parse status;
- hook trust status when safely detectable, otherwise `unknown`;
- actionable remediation such as `ai_dev_loop integrations install` or "open `/hooks` in Codex and trust the hook".

`doctor` must remain non-mutating.

### Real Installation In This Phase

Because the user approved installing in this phase, after implementation and automated validation:

1. Run the install command against the real user HOME:

   ```bash
   uv run ai_dev_loop integrations install
   ```

2. Run:

   ```bash
   uv run ai_dev_loop integrations status
   uv run ai_dev_loop doctor
   ```

3. Report the installed paths and trust instructions.
4. Do not attempt to open `/hooks` or mark the hook trusted automatically.
5. If real installation fails due to invalid existing user files, stop safely and report the path and remediation. Do not overwrite invalid files.

## Implementation Plan

1. Add integration asset files.
   - Keep `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc` aligned with implementation decisions.
   - Create package-owned handoff skill and SessionStart hook script.
   - Add package data configuration if needed so assets are present in built distributions.
   - Keep hook script standalone and standard-library-only.

2. Add an integration support module.
   - Suggested module: `src/ai_dev_loop/integrations/codex/install.py` or `src/ai_dev_loop/codex_integration.py`.
   - Define typed result objects for asset status, install, and uninstall.
   - Centralize paths:
     - skill file;
     - hook script;
     - hooks JSON;
     - backups;
     - hook session record directory.
   - Use `Path.home()` for global Codex asset paths and existing XDG helpers for app state/config/cache paths.

3. Implement secure writes and comparisons.
   - Use existing atomic write helpers where available.
   - Apply directory `0700` and sensitive file `0600` where supported.
   - Compare installed content to package content by SHA-256 or exact text bytes.
   - Avoid rewriting files that are already current unless a backup/update is needed.

4. Implement hook JSON merge.
   - Load and validate JSON.
   - Treat missing file as an empty hooks object.
   - Preserve unrelated content.
   - Add the SessionStart entry if missing.
   - If an older or changed `ai_dev_loop` hook entry exists, update it to the current command.
   - Avoid duplicates.
   - Back up before writing any change to an existing file.
   - Add unit tests for empty, unrelated hooks, existing matching hook, existing changed hook, duplicate cleanup, invalid JSON, and uninstall preserving unrelated hooks.

5. Implement hook JSON uninstall.
   - Remove only the command hook whose command matches the installed `ai_dev_loop` hook script path or deterministic `ai_dev_loop` hook identity.
   - Remove now-empty hook arrays/entries only when they were made empty by removing `ai_dev_loop`.
   - Preserve all unrelated hooks and top-level keys.
   - Back up before writing.
   - Do not delete invalid JSON. Fail safely with a clear message.

6. Implement SessionStart hook behavior.
   - Add tests that run the installed script as a subprocess with sample JSON stdin.
   - Cover valid `startup`, `resume`, `clear`, and `compact`.
   - Cover incomplete input, invalid JSON, unsupported source, missing session ID, and missing transcript path.
   - Verify the hook writes only metadata and never attempts to read transcript contents.
   - Verify returned JSON includes `hookSpecificOutput.additionalContext`.
   - Verify session record permissions where practical.

7. Implement integration commands.
   - Replace `raise_not_implemented()` usage for install/uninstall.
   - Wire Typer command options if adding `--output json`.
   - Keep output concise and actionable.
   - Include hook trust instructions in install output.
   - Ensure exit codes use existing `AiDevLoopError` conventions.

8. Improve `integrations status`.
   - Report exact installed paths and match/current state.
   - JSON output should be stable and machine-readable.
   - Text output should avoid dumping full hook JSON.
   - If `hooks.json` is invalid, status should report that without modifying it.

9. Update `doctor`.
   - Add non-mutating checks for the same assets.
   - Avoid printing full hook JSON.
   - Include actionable remediation.
   - Keep `doctor --output json` stable.

10. Update README.
    - Replace "Phase 7 pending" text with actual integration workflow.
    - Document:
      - WSL install;
      - `uv tool install .` and optional `pipx install .`;
      - `ai_dev_loop integrations install`;
      - `/hooks` trust step;
      - `integrations status`;
      - target repository `ai_dev_loop.yaml`;
      - generic handoff skill usage;
      - project planning skill integration;
      - prepare/start handoff;
      - complete loop behavior;
      - status/resume/abort usage;
      - privacy and cleanup;
      - uninstall behavior.

11. Add tests.
    - Unit tests for asset path derivation with temporary `HOME`.
    - Unit tests for package asset loading.
    - Unit tests for skill install/status matching.
    - Unit tests for hook script install/status matching.
    - Unit tests for hook JSON merge/idempotency/backup/uninstall.
    - Unit tests for hook input/output.
    - Unit tests for doctor integration checks.
    - Integration tests using temporary `HOME` and XDG dirs:
      - first-time install;
      - idempotent second install;
      - install with existing unrelated hooks;
      - uninstall preserving unrelated hooks;
      - status JSON after install and uninstall;
      - invalid hooks JSON fails safely without overwrite.
    - Do not invoke real model activity.

12. Validate.
    - Run focused tests first.
    - Then run the full suite:

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

13. Install into the real user HOME.
    - Only after the implementation and test suite pass, run:

```bash
uv run ai_dev_loop integrations install
uv run ai_dev_loop integrations status
uv run ai_dev_loop doctor
```

    - Report the actual installed paths.
    - Tell the user to open `/hooks` in Codex and trust the `ai_dev_loop` hook.
    - Do not automatically bypass trust.

## Acceptance Criteria

- `ai_dev_loop integrations install` no longer returns the placeholder exit code.
- `ai_dev_loop integrations uninstall` no longer returns the placeholder exit code.
- Install creates or updates the global handoff skill under `$HOME/.agents/skills/ai-dev-loop-handoff/SKILL.md`.
- Install creates or updates the SessionStart hook script under `$HOME/.codex/hooks/ai_dev_loop_session_start.py`.
- Install safely merges the SessionStart registration into `$HOME/.codex/hooks.json`.
- Install is idempotent.
- Install preserves unrelated hooks.
- Install backs up an existing `hooks.json` before modifying it.
- Install tells the user to trust the hook through `/hooks`.
- The SessionStart hook returns valid JSON with additional context.
- The SessionStart hook writes only minimal session metadata under XDG state.
- The SessionStart hook never reads transcript contents.
- `integrations status --output json` reports installed paths and match status.
- `integrations uninstall` removes only `ai_dev_loop` assets and preserves unrelated hooks.
- `doctor` reports integration status and actionable remediation without mutating files.
- Tests use temporary `HOME` and do not touch real user Codex files except in the final approved install step.
- README documents installation, trust, status, uninstall, privacy, and cleanup.
- Full validation passes.
- The final real install succeeds or fails safely without overwriting invalid user files.

## Open Questions

None. The user explicitly approved performing the real global install during this phase. If existing real user hook files are invalid or ambiguous, stop safely and report remediation rather than overwriting them.
