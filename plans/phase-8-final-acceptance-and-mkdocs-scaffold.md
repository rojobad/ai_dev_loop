# Phase 8 Plan: Final Acceptance And MkDocs Scaffold

## Goal Or Goals

Complete final acceptance hardening for `ai_dev_loop` after Phases 0 through 7.5.

Phase 8 should prove the implemented orchestrator works as a package and on the user's real workstation, including real Codex Desktop/WSL integration installation and real model-backed acceptance where appropriate.

This phase must also add a MkDocs documentation scaffold under `/docs`, but it must not write the final product documentation. Final documentation content is reserved for Phase 8.5.

At the end of Phase 8:

- automated tests, linting, typing, package build, and CLI smoke checks pass;
- a disposable end-to-end acceptance test exists and passes with fake Cursor/Codex CLIs;
- package installation outside editable/dev mode is verified;
- real workstation integration for `codex-desktop-wsl` is installed and verified;
- real model-backed smoke validation is performed only for the explicitly approved acceptance paths in this plan;
- MkDocs is configured and buildable;
- `/docs` exists with one small test page only;
- the untracked bridge reference markdown is folded into the official MkDocs scaffold or removed after its useful content is represented elsewhere;
- no final documentation prose is produced beyond placeholders required to prove MkDocs works.

## User Decisions Already Made

The user approved these decisions before Phase 8:

- The untracked shell helper `scripts/share-codex-desktop-sessions.sh` was deleted and must not be restored as a product surface.
- The remaining reference file `scripts/codex-desktop-wsl-sessions.md` should be folded into official documentation structure, but final documentation writing is deferred to Phase 8.5.
- Real installation on the user's workstation is allowed in Phase 8.
- Real model calls are allowed in Phase 8.
- Documentation must use MkDocs, configured in the project root with source files under `/docs`.
- The MkDocs site should use Material for MkDocs.
- Cursor should execute Phase 8 except for writing final documentation content.
- Phase 8.5 will create the final documentation using another model.
- If WSL distro auto-detection is ambiguous, use `Ubuntu-22.04`.
- The Codex Desktop `/hooks` trust step is already complete.
- If no exact desktop session ID is available for real model-backed smoke validation, stop instead of guessing.
- Move `scripts/codex-desktop-wsl-sessions.md` into `docs/reference/` as a placeholder for Phase 8.5.
- Do not implement an explicit destructive state-cleanup command; cleanup remains a manual documentation policy.

## Non-Goals

- Do not write the final user-facing documentation set.
- Do not turn `README.md` into the final docs site.
- Do not generate long documentation pages for architecture, usage, troubleshooting, privacy, or cleanup.
- Do not restore the deleted shell bridge helper.
- Do not replace the typed Python `ai_dev_loop integrations sessions ...` product surface with shell scripts.
- Do not create commits, tags, pushes, resets, cleans, stashes, unstaging, or destructive cleanup.
- Do not change core loop semantics unless a final acceptance finding proves a bug.
- Do not use `--last` for Codex handoff or review recovery.
- Do not infer, shorten, guess, or substitute Codex session IDs.
- Do not share the whole Windows `.codex` home with WSL.
- Do not set WSL `CODEX_HOME` to `/mnt/c/.../.codex`.
- Do not symlink, copy, migrate, open, or inspect Codex SQLite databases across Windows and WSL.
- Do not make `integrations uninstall --target codex-desktop-wsl` remove the desktop session bridge by default.
- Do not bypass or mutate Codex hook trust state.

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
- `phase-7-5-findings.md`
- `plans/phase-6-real-abort.md`
- `plans/phase-7-global-codex-integrations.md`
- `plans/phase-7-5-codex-desktop-wsl-bridge.md`
- `plans/prompt_phase-7-global-codex-integrations.txt`
- `plans/prompt_phase-7-5-codex-desktop-wsl-bridge.txt`
- `scripts/codex-desktop-wsl-sessions.md`
- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`
- `README.md`
- `pyproject.toml`

Also use the MkDocs official documentation for current command/config behavior:

- <https://www.mkdocs.org/>
- <https://www.mkdocs.org/user-guide/configuration/>
- <https://www.mkdocs.org/user-guide/cli/>
- <https://squidfunk.github.io/mkdocs-material/getting-started/>

## Cursor Rules And Skills

Cursor must follow these repo-local governance inputs during Phase 8:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.agents/skills/create-cursor-plan/SKILL.md`
- `.agents/skills/review-staged-changes/SKILL.md`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`

The new Cursor rule `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc` is mandatory for Phase 8 implementation work. It preserves the MkDocs scaffold, real workstation, model-smoke, hook-trust, session-ID, bridge, cleanup, and findings-handoff decisions made for final acceptance.

The Codex skill `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` is the companion governance skill for Codex-authored Phase 8.5 documentation and Codex review of Phase 8 acceptance findings.

## Current Repository Facts

At the start of Phase 8, the repository has:

- a Python package under `src/ai_dev_loop`;
- test coverage for prepare, start, staging, Codex review, bounded loop, resume, abort, integrations, and desktop bridge behavior;
- `uv.lock` and `pyproject.toml`;
- a short README that documents current behavior but is not the final docs site;
- no `docs/` directory yet;
- no `mkdocs.yml` yet;
- one untracked reference file:

  ```text
  scripts/codex-desktop-wsl-sessions.md
  ```

The deleted file below must stay deleted:

```text
scripts/share-codex-desktop-sessions.sh
```

Phase 7.5 reported that the full suite passed with native WSL temp variables:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
```

It also reported that pytest capture can fail before collection when process-level temp variables point at DrvFS. Prefer native WSL temp for full-suite validation.

## Scope

This phase owns:

1. Final automated acceptance hardening.
2. Disposable end-to-end acceptance test coverage.
3. Package install validation outside editable/dev mode.
4. Real workstation `codex-desktop-wsl` install/status/session bridge validation.
5. Real model-backed smoke validation for the handoff/review path, bounded so it does not perform large implementation work.
6. MkDocs dependency and configuration.
7. A minimal `/docs/index.md` test page proving the docs site builds.
8. A docs placeholder structure that Phase 8.5 can fill.
9. Cleanup or migration of `scripts/codex-desktop-wsl-sessions.md` so it is not left as an untracked product-adjacent artifact.
10. Final README adjustment only where necessary to point users to the future MkDocs site and current commands.

## Architecture Guardrails

- `ai_dev_loop` remains a deterministic local orchestrator.
- Codex remains architect, context owner, prompt author, and reviewer.
- Cursor remains implementation agent.
- The original Codex session remains the only authoritative reviewer.
- Every automated review uses `codex exec resume <exact-session-id>`.
- Every Cursor implementation/fix turn uses the same stored Cursor chat ID.
- The orchestrator never authors or rewrites Cursor correction prompts.
- Loop decisions come from schema-validated Codex JSON, not Markdown scraping.
- State remains under XDG state outside target repositories.
- The target repository must not contain run state, session logs, or agent output.
- Final changes from acceptance repos remain staged; do not commit them.
- Preserve enough evidence for manual recovery.
- Use subprocess argument arrays with `shell=False`.
- Do not log complete environments, auth payloads, tokens, full prompts, full staged patches, raw JSONL, or transcript contents in default output.
- Keep hook trust manual.
- Keep desktop session bridge limited to rollout session files.

## MkDocs Requirements

Add MkDocs without writing final docs content.

Implement:

- Add a focused docs dependency, preferably under the existing dev optional dependencies:

  ```toml
  "mkdocs>=1.6"
  "mkdocs-material>=9.0,<10.0"
  ```

- Add root-level `mkdocs.yml`.
- Add `/docs/index.md` as a short test page.
- Add `docs/reference/codex-desktop-wsl-sessions.md` as a placeholder moved from `scripts/codex-desktop-wsl-sessions.md`.
- Optionally add placeholder pages only if needed to prove nav structure, but keep them clearly marked as Phase 8.5 placeholders.
- Ensure `uv run mkdocs build --strict` passes.
- Ensure generated site output is not tracked. If needed, add `site/` to `.gitignore`.
- Do not generate real documentation pages in this phase.

Suggested minimal `mkdocs.yml` responsibilities:

- `site_name: ai_dev_loop`
- simple nav containing `Home` and the reference placeholder only if needed for strict builds;
- Material theme configured with:

  ```yaml
  theme:
    name: material
  ```

- docs directory remains `docs`.

The Phase 8.5 documentation model will expand the docs site with architecture, install, workflow, integration targets, troubleshooting, privacy, cleanup, and API/CLI reference content.

## Reference Markdown Handling

The file below is legacy reference material, not a product interface:

```text
scripts/codex-desktop-wsl-sessions.md
```

In Phase 8:

1. Read it.
2. Preserve any unique safety facts as comments, issue notes, or Phase 8.5 doc TODOs in the MkDocs scaffold.
3. Do not copy its outdated project-specific references, especially references to `CryptoSentinel` or GitHub URLs from another project.
4. Move it to:

   ```text
   docs/reference/codex-desktop-wsl-sessions.md
   ```

5. Convert it into a placeholder page for Phase 8.5 rather than final documentation.
6. Remove or rewrite outdated project-specific references during the move.

Do not recreate `scripts/share-codex-desktop-sessions.sh`.

## Disposable End-To-End Acceptance Test

Add or complete an end-to-end local acceptance test that uses fake CLIs and a disposable temporary Git repository. It must not require paid model calls.

The test should cover:

1. install or invoke the package CLI;
2. install global integration assets into temporary WSL and Windows-style homes;
3. create a valid `ai_dev_loop.yaml`;
4. create a plan and prompt file;
5. run `prepare` with prompt content through stdin;
6. run `start` with fake Cursor and fake Codex CLIs;
7. simulate at least one stage-review-fix-review path;
8. verify the same Cursor chat ID is reused;
9. verify the same Codex session ID is used for every review;
10. verify final staged changes;
11. verify run artifacts and state;
12. verify no state files were created inside the target repository;
13. verify desktop integration fixtures do not symlink, copy, open, or inspect SQLite state;
14. verify uninstall preserves unrelated hooks and leaves session bridge unless explicitly removed.

Keep real model activity out of automated tests. Real model checks belong in the manual acceptance section below.

## Package Install Validation

Verify at least one non-editable install path.

Recommended approach:

1. Build the package:

   ```bash
   uv run python -m build
   ```

2. Install the generated wheel into a temporary virtual environment or with `uv tool install --force dist/*.whl`.
3. Verify:

   ```bash
   ai_dev_loop --help
   ai_dev_loop doctor
   ai_dev_loop integrations status --target wsl-cli
   ai_dev_loop integrations status --target codex-desktop-wsl
   ai_dev_loop integrations sessions status
   ai_dev_loop config validate --repo tests/fixtures/sample_repo
   ```

4. If `uv tool install` cannot install from wildcard path directly, use a concrete wheel path.
5. Do not overwrite unrelated user-installed tools without a clear command and confirmation in the findings.

## Real Workstation Acceptance

The user approved real workstation installation and real model calls for Phase 8.

Perform real workstation checks carefully and record exact commands and outcomes in `phase-8-findings.md`.

### Real Integration Install

Run:

```bash
uv run ai_dev_loop integrations status --target codex-desktop-wsl
uv run ai_dev_loop integrations sessions status
uv run ai_dev_loop integrations sessions list
uv run ai_dev_loop integrations install --target codex-desktop-wsl
uv run ai_dev_loop integrations status --target codex-desktop-wsl
```

If WSL distro detection is ambiguous, pass:

```bash
--wsl-distro Ubuntu-22.04
```

If Windows Codex home detection is ambiguous, pass:

```bash
--windows-codex-home "/mnt/c/Users/<windows-user>/.codex"
```

Do not pass `--install-session-bridge` unless the bridge is missing and installing it is necessary for acceptance.

The known manual bridge may already exist:

```text
/home/rojobad/.codex/sessions/from-desktop -> /mnt/c/Users/Rodrigo Badia/.codex/sessions
```

If the bridge exists and is healthy, preserve it.

### Hook Trust

The user has reported that the Codex Desktop `/hooks` trust step is already complete.

Cursor should:

- print the exact trust instruction;
- verify status as far as documented and safely detectable;
- record that trust was user-confirmed before Phase 8;
- pause only if the real install changes the hook definition in a way that would require renewed trust;
- not attempt to bypass or edit hook trust state.

If an unattended run cannot verify whether the real install changed the trusted hook definition, record this as a manual acceptance caveat in `phase-8-findings.md` rather than guessing.

### Real Session Context Verification

After hook trust, verify that a new or resumed Codex Desktop session receives `ai_dev_loop` SessionStart context containing the exact session ID.

If this cannot be verified automatically, ask the user to confirm the SessionStart context is visible in Codex Desktop.

Do not read transcript contents.

### Real Desktop Session Bridge Verification

Verify:

```bash
uv run ai_dev_loop integrations sessions status
uv run ai_dev_loop integrations sessions list
```

The list command may read rollout filenames only. It must not read transcript contents beyond filename metadata.

If safe and necessary, verify that WSL Codex CLI can resolve a desktop session ID:

```bash
codex exec resume <exact-desktop-session-id> --help
```

If a real `codex exec resume <session-id>` call would send a prompt or consume model tokens, only do it in the model-backed smoke test below.

## Real Model-Backed Smoke Validation

The user approved real model calls.

Keep this validation small and bounded. The goal is not to implement a large feature; the goal is to prove the real Cursor/Codex/Codex Desktop handoff path works.

Recommended smoke path:

1. Create a disposable temporary Git repository under native WSL storage.
2. Add a tiny Python package or text-file project.
3. Add `ai_dev_loop.yaml` with conservative timeouts and `max_review_iterations: 1` or `2`.
4. Start or resume a real Codex Desktop session after hook trust.
5. Use the handoff skill or manual `prepare` with the exact session ID.
6. Use a tiny Cursor prompt that makes a low-risk local edit, such as adding one function and one test.
7. Exit or stop using the active Codex UI for that session.
8. Run `uv run ai_dev_loop start <run-id>` from WSL.
9. Confirm:
   - Cursor creates exactly one chat;
   - Codex review resumes the exact session ID;
   - final changes are staged;
   - run artifacts are under XDG state;
   - no state artifacts are written inside the disposable repo.

Stop immediately and preserve artifacts if:

- Cursor attempts unexpected broad edits;
- Codex session ID is missing or mismatched;
- no exact desktop session ID is available;
- hook context is missing after trust and restart/resume;
- the worktree safety policy rejects unexpected drift;
- model output returns invalid structured review JSON;
- any command would require guessing a session ID.

Do not run this smoke test against the real `ai_dev_loop` source repository.

## CLI And UX Acceptance

Verify:

```bash
uv run ai_dev_loop --help
uv run ai_dev_loop prepare --help
uv run ai_dev_loop start --help
uv run ai_dev_loop resume --help
uv run ai_dev_loop abort --help
uv run ai_dev_loop status --help
uv run ai_dev_loop logs --help
uv run ai_dev_loop inspect --help
uv run ai_dev_loop list --help
uv run ai_dev_loop doctor --help
uv run ai_dev_loop integrations --help
uv run ai_dev_loop integrations install --help
uv run ai_dev_loop integrations uninstall --help
uv run ai_dev_loop integrations status --help
uv run ai_dev_loop integrations sessions --help
uv run ai_dev_loop config validate --help
```

Fix help text only when it is inaccurate or materially confusing.

## Status, Logs, Inspect, And List Final Polish

Review the existing implementations against the master plan:

- `status` should show status, repo, branch, initial HEAD, current/max iterations, Cursor chat ID, shortened Codex session ID, active child process, latest error, and next safe action.
- `logs` should support following and component filtering.
- `inspect` should show artifact paths and avoid printing sensitive prompt/review content unless an explicit flag requests it.
- `list` should show recent runs with filters for project, status, repository, and date where implemented.

If any of these are still placeholders or materially incomplete, implement the missing behavior with focused tests.

Do not broaden output to include full prompts, full fix prompts, full staged patches, raw JSONL, or transcript contents by default.

## Doctor Final Polish

Review `doctor` against the master plan.

It should verify or report:

- Python/runtime version;
- state/config/cache directory permissions;
- `git`;
- Cursor CLI executable and version;
- Cursor authentication status when safely detectable;
- available Cursor models when safely detectable;
- Codex CLI executable and version;
- Codex authentication status when safely detectable;
- Codex hook installation;
- hook trust status as unknown when not safely detectable;
- global skill installation;
- JSON schema availability;
- target repository configuration when `--repo` is supplied;
- Windows Codex Desktop home detection for `codex-desktop-wsl`;
- WSL distro and `wsl.exe` availability for `codex-desktop-wsl`;
- desktop session bridge status for `codex-desktop-wsl`.

Doctor must remain read-only unless an explicit install or repair flag exists.

## Tests

Add or update tests for any code changes.

Required final validation:

```bash
git diff --check
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

If DrvFS temp behavior is intentionally simulated, run those tests separately with `-s` and record the result.

## Deliverables

Create or update:

- `pyproject.toml` with MkDocs dev dependency.
- `uv.lock` if dependency resolution changes it.
- `mkdocs.yml`.
- `docs/index.md`.
- `docs/reference/codex-desktop-wsl-sessions.md` as a placeholder moved from `scripts/`.
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`.
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`.
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/agents/openai.yaml`.
- `.gitignore` if `site/` is not already ignored.
- tests for any final acceptance gaps.
- E2E fake-agent/fake-codex acceptance test if not already present.
- `phase-8-findings.md` with commands, outcomes, real install notes, real model smoke notes, residual risks, and Phase 8.5 handoff.
- README only for minimal handoff to MkDocs and any command corrections.

Do not create final docs pages in this phase.

## Phase 8.5 Handoff Requirements

At the end of Phase 8, `phase-8-findings.md` must tell the Phase 8.5 documentation model:

- MkDocs is configured.
- The docs build command is `uv run mkdocs build --strict`.
- Which placeholder pages exist.
- Which final documentation pages should be written.
- Which facts from `scripts/codex-desktop-wsl-sessions.md` were preserved.
- That the reference markdown file was moved to `docs/reference/`.
- Any real workstation quirks discovered during install or model smoke validation.
- That cleanup remains manual and no destructive cleanup command was implemented.

Suggested Phase 8.5 docs outline:

- architecture;
- installation in WSL;
- integration target selection;
- Codex Desktop on Windows with WSL bridge;
- hook trust;
- target repository configuration;
- handoff workflow;
- prepare/start/resume/abort;
- status/logs/inspect/list;
- state and artifact layout;
- safety model;
- troubleshooting;
- privacy and cleanup;
- uninstall.

## Open Questions

None currently known.

Resolved decisions for Phase 8:

- Use `Ubuntu-22.04` if WSL distro detection is ambiguous.
- Treat Codex Desktop `/hooks` trust as already completed unless the install reports a changed hook definition requiring renewed trust.
- Stop if no exact desktop session ID is available for real model-backed smoke validation.
- Move `scripts/codex-desktop-wsl-sessions.md` to `docs/reference/codex-desktop-wsl-sessions.md` as a Phase 8.5 placeholder.
- Use Material for MkDocs.
- Do not implement a destructive cleanup command.

## Acceptance Criteria

- `scripts/share-codex-desktop-sessions.sh` remains deleted.
- The Cursor docs/acceptance rule exists and is referenced by the Phase 8 plan and prompt.
- The Codex docs/acceptance governance skill exists and is referenced by the Phase 8 plan as the Phase 8.5 documentation companion.
- `scripts/codex-desktop-wsl-sessions.md` is no longer an untracked loose artifact because it was moved intentionally into `docs/reference/`.
- `mkdocs.yml` exists.
- `/docs/index.md` exists and is intentionally minimal.
- Material for MkDocs is installed/configured.
- `uv run mkdocs build --strict` passes.
- Full automated validation passes with native WSL temp variables.
- Package build passes.
- Non-editable package install is verified.
- Disposable fake-agent/fake-codex E2E acceptance passes.
- Real `codex-desktop-wsl` install/status checks are performed or blocked only on an explicitly recorded user/manual step.
- Real model-backed smoke validation is performed or blocked only on an explicitly recorded user/manual step.
- No real acceptance run mutates the source repository unexpectedly.
- No final documentation prose is generated beyond the MkDocs test page and Phase 8.5 placeholders.
- `phase-8-findings.md` records all commands, outcomes, residual risks, and Phase 8.5 handoff details.
