# Phase 8 Findings

This file summarizes Phase 8 final acceptance hardening, MkDocs scaffolding, package
validation, real workstation checks, and bounded real model smoke validation. It is
the handoff artifact for Phase 8.5 documentation authoring.

## Scope Completed

Phase 8 delivered:

- MkDocs + Material for MkDocs dev dependencies and root `mkdocs.yml`
- Minimal `docs/index.md` test page
- Placeholder `docs/reference/codex-desktop-wsl-sessions.md` moved from
  `scripts/codex-desktop-wsl-sessions.md`
- `site/` added to `.gitignore`
- Disposable E2E acceptance test with fake Cursor/Codex CLIs
- Cursor CLI probe fixes discovered during real smoke validation
- Full automated validation with native WSL temp variables
- Non-editable wheel install via `uv tool install --force`
- Real `codex-desktop-wsl` integration status/install verification
- Bounded real model smoke validation in a disposable repository

Phase 8 did **not** write final documentation prose beyond placeholders.

`scripts/share-codex-desktop-sessions.sh` remains deleted and was not restored.

## Commands Run

### Automated validation (all passed)

```bash
git diff --check
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

Final counts: **260 passed**; package build produced
`dist/ai_dev_loop-0.1.0-py3-none-any.whl`; MkDocs strict build succeeded.

### MkDocs scaffold

```bash
uv sync --all-extras
uv run mkdocs build --strict
```

Configuration:

- `mkdocs.yml` with `theme.name: material`
- docs source under `docs/`
- nav: Home + reference placeholder only

### Package install validation

```bash
uv run python -m build
uv tool install --force dist/ai_dev_loop-0.1.0-py3-none-any.whl
```

Installed tool smoke checks (from `~/.local/bin/ai_dev_loop`):

```bash
ai_dev_loop --help
ai_dev_loop doctor
ai_dev_loop integrations status --target wsl-cli
ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
ai_dev_loop integrations sessions status
ai_dev_loop config validate \
  --repo /home/rojobad/Projects/ai_dev_loop/tests/fixtures/sample_repo \
  --config-path /home/rojobad/Projects/ai_dev_loop/tests/fixtures/sample_repo/ai_dev_loop.yaml
```

Notes:

- `uv tool install --force` replaced the prior user-level `ai_dev_loop` tool with
  the Phase 8 wheel build.
- `config validate --repo tests/fixtures/sample_repo` alone resolves to the
  enclosing `ai_dev_loop` git root because the fixture is not an independent git
  repository. Use `--config-path` to validate the fixture config explicitly.

All CLI `--help` commands for prepare/start/resume/abort/status/logs/inspect/list/doctor/integrations/config exited 0.

### Real workstation integration

```bash
uv run ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
uv run ai_dev_loop integrations sessions status
uv run ai_dev_loop integrations sessions list
uv run ai_dev_loop integrations install --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
uv run ai_dev_loop integrations status --target codex-desktop-wsl --wsl-distro Ubuntu-22.04
codex exec resume 019f3e60-…-e9ed082 --help
```

Results:

- Desktop target skill, hook script, Windows `hooks.json`, and WSL hook script
  were already installed and idempotent reinstall reported all `current`.
- Hook command unchanged:
  `wsl.exe -d Ubuntu-22.04 --exec python3 /home/rojobad/.codex/hooks/ai_dev_loop_session_start.py`
- Session bridge healthy:
  `/home/rojobad/.codex/sessions/from-desktop` ->
  `/mnt/c/Users/Rodrigo Badia/.codex/sessions`
- Bridge target matches desktop sessions directory; 122 reachable rollout files.
- Hook trust status: `unknown` (not safely detectable from local files).
- User reported Codex Desktop `/hooks` trust was already complete before Phase 8;
  idempotent install did not change hook definition, so renewed trust was not
  required.
- WSL Codex CLI resolves desktop session IDs through the bridge (`codex exec
  resume <id> --help` succeeded).

### Real model-backed smoke validation

Disposable repository (not the `ai_dev_loop` source tree):

```text
/tmp/ai_dev_loop_phase8_smoke_retry
```

Desktop session ID used (exact value from `integrations sessions list`; redacted
here — full ID remains in local XDG run artifacts only):

```text
019f3e60-…-e9ed082
```

Prepare:

```bash
uv run ai_dev_loop prepare \
  --repo-path /tmp/ai_dev_loop_phase8_smoke_retry \
  --plan-path docs/plans/smoke-plan.md \
  --prompt-source-path docs/plans/prompt_smoke-plan.txt \
  --codex-session-id <desktop-session-id> \
  --output json < /tmp/ai_dev_loop_phase8_smoke_retry/docs/plans/prompt_smoke-plan.txt
```

Run:

```bash
export PATH="$HOME/.local/bin:$PATH"
uv run ai_dev_loop start <run-id>
```

Outcome: **`max_iterations_reached`** (expected with `max_review_iterations: 1`
and actionable Codex findings).

Verified:

- Exactly one Cursor chat created (chat ID redacted; see local `cursor/chat.json`)
- Codex review resumed the exact prepared desktop session ID (redacted here;
  recorded in run `state.json` and Codex review metadata)
- Structured review JSON validated (`has_actionable_findings: true`,
  `findings_count: 1`, fix prompt persisted)
- Staged changes remain in disposable repo; no `state.json` inside target repo
- Run artifacts under `$XDG_STATE_HOME/ai_dev_loop/runs/<project>/<run-id>/`
  (exact paths remain local only)

Earlier smoke attempts (recorded for acceptance audit):

1. **Probe failure (fixed in Phase 8):** Cursor auth/model probes did not match
   real `agent status --format json` (`isAuthenticated`) or formatted `agent
   models` output (`composer-2.5-fast - Composer 2.5 Fast`).
2. **Model config failure:** default sample `review_model: o4-mini` is not
   supported on this ChatGPT-linked Codex account. Retry with `gpt-5.5` succeeded
   for review execution.

## Code Changes

| Area | Change |
| --- | --- |
| `pyproject.toml` | Added `mkdocs>=1.6,<2.0`, `mkdocs-material>=9.0,<10.0` dev deps |
| `mkdocs.yml` | Material theme, minimal nav |
| `docs/index.md` | Scaffold test page |
| `docs/reference/codex-desktop-wsl-sessions.md` | Phase 8.5 placeholder with preserved safety facts |
| `.gitignore` | Ignore generated `site/` |
| `README.md` | Minimal MkDocs pointer and build command |
| `tests/integration/test_e2e_acceptance.py` | Full fake-CLI E2E acceptance |
| `src/ai_dev_loop/runners/probes.py` | Fix Cursor auth/model probe parsing |
| `tests/unit/test_probes.py` | Probe regression tests |
| `scripts/codex-desktop-wsl-sessions.md` | Removed after move to `docs/reference/` |

Governance files referenced by the plan already existed before implementation:

- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/agents/openai.yaml`

## Reference Markdown Migration

Safety facts preserved in `docs/reference/codex-desktop-wsl-sessions.md`:

- Separate WSL and Windows `.codex` homes
- Whole-home sharing and `/mnt/c` `CODEX_HOME` are unsafe (SQLite/schema risks)
- Nested `from-desktop` symlink is the supported bridge
- Resume desktop chats by exact session ID only
- Concurrent edit caution across desktop and WSL
- Python CLI is the product surface (`integrations sessions ...`)

Removed during migration:

- `CryptoSentinel` references
- GitHub URLs from another project
- Deleted shell helper `scripts/share-codex-desktop-sessions.sh` instructions

## Residual Risks

1. **Hook trust detection remains `unknown`.** Phase 8.5 docs should keep manual
   `/hooks` trust steps for Codex Desktop.
2. **WSL `wsl-cli` skill mismatch.** Doctor reports
   `~/.agents/skills/ai-dev-loop-handoff/SKILL.md` differs from current package
   content while the desktop target skill matches. Optional remediation:
   `ai_dev_loop integrations install --target wsl-cli`.
3. **Default `review_model: o4-mini` may fail on ChatGPT-linked Codex accounts.**
   Phase 8.5 docs should note choosing a supported review model (for example
   `gpt-5.5`) in target `ai_dev_loop.yaml`.
4. **MkDocs 2.0 upstream warning.** Material for MkDocs prints a forward-looking
   warning about MkDocs 2.0; current pin `mkdocs>=1.6,<2` is intentional for Phase
   8.
5. **`config validate --repo` on nested fixtures** resolves to the enclosing git
   root when the path is not its own repository. Document `--config-path` for
   fixture validation.
6. **No destructive cleanup command** was implemented; cleanup remains manual per
   governance.

## Phase 8.5 Documentation Handoff

### Build command

```bash
uv sync --all-extras
uv run mkdocs build --strict
```

### Existing placeholder pages

| Path | Purpose |
| --- | --- |
| `docs/index.md` | Scaffold home / build instructions |
| `docs/reference/codex-desktop-wsl-sessions.md` | Desktop bridge safety placeholder |

### Pages Phase 8.5 should write

- architecture
- installation in WSL
- integration target selection (`wsl-cli` vs `codex-desktop-wsl`)
- Codex Desktop on Windows with WSL bridge
- hook trust (`/hooks` in Codex Desktop)
- target repository configuration (`ai_dev_loop.yaml`, review model selection)
- handoff workflow (prepare stdin, exit Codex, start)
- prepare / start / resume / abort
- status / logs / inspect / list
- state and artifact layout under XDG paths
- safety model (staging, no git destructive ops, agent output untrusted)
- troubleshooting (bridge mispoint, model unsupported, probe/auth issues)
- privacy and manual cleanup policy
- uninstall (integrations and bridge management)
- CLI reference

### Real workstation quirks to document

- Use `Ubuntu-22.04` for `--wsl-distro` when auto-detection is ambiguous.
- Existing healthy bridge was preserved; install does not recreate bridge by
  default.
- Desktop hook command uses `wsl.exe -d Ubuntu-22.04 --exec python3 ...`.
- Real smoke validation required `review_model: gpt-5.5` on this workstation;
  `o4-mini` returned HTTP 400 for ChatGPT-linked accounts.

### Cleanup policy

No destructive cleanup command exists. Phase 8.5 should document manual removal
of XDG run state, integration assets, and session bridge when users choose to
clean up.
