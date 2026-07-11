# Phase 9 - Optional Codex Review Model And Reasoning

## Goal

Allow automated Codex reviews to inherit the model and reasoning configuration of the exact resumed Codex session by default, while still allowing a target repository or `prepare` invocation to override either value explicitly.

After implementation:

- `codex.review_model` is optional.
- `codex.review_reasoning_effort` is optional.
- If `review_model` is absent or `null`, `ai_dev_loop` omits `--model` from `codex exec resume`, allowing Codex to resume with the session's model.
- If `review_reasoning_effort` is absent or `null`, `ai_dev_loop` omits the `model_reasoning_effort` override, allowing Codex to resume with the session's reasoning configuration.
- Either setting can be overridden independently.
- The updated package is built, tested, and installed into the current WSL user environment as the active `ai_dev_loop` tool.

## Non-Goals

- Do not discover or parse the original model or reasoning effort from Codex rollout files.
- Do not duplicate model-selection logic inside `ai_dev_loop`.
- Do not validate model availability by sending a real model request.
- Do not change Cursor model selection.
- Do not change the review schema, finding extraction, correction loop, or review prompt semantics.
- Do not introduce a separate `inherit_session_model` boolean. Absence of an override is the inheritance contract.

## Scope

- Project configuration models, defaults, precedence, and JSON Schema.
- `prepare` CLI overrides and persisted run state.
- Codex review command construction and redaction-safe diagnostics.
- Backward compatibility for existing repository configs and persisted runs.
- Unit, integration, schema, CLI, and documentation tests affected by the configuration change.
- User-facing MkDocs documentation, examples, troubleshooting, reference material, and root README where relevant.
- Build and WSL installation of the verified package.

## Out of Scope

- Target repository application code outside its `ai_dev_loop.yaml` examples or fixtures.
- Codex Desktop settings, session bridge implementation, hook installation, and skill installation.
- Cursor Agent authentication and model probes.
- Real Cursor or Codex model execution during automated tests.
- Starting, resuming, aborting, or otherwise mutating an existing user run as part of installation validation.
- Commits, pushes, tags, releases, or publication to a package index.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `archive/implementation-history/plans/phase-4-codex-review-runner.md`
- `src/ai_dev_loop/config.py`
- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/commands/prepare.py`
- `src/ai_dev_loop/cli.py`
- `src/ai_dev_loop/runners/codex.py`
- `src/ai_dev_loop/schemas/project-config-v1.json`
- `src/ai_dev_loop/schemas/run-state-v1.json`
- `tests/unit/test_config.py`
- `tests/unit/test_codex_runner.py`
- `tests/fixtures/sample_repo/ai_dev_loop.yaml`
- `docs/referencia/configuracion.md`
- `docs/guia/configuracion-repositorio.md`
- `docs/guia/guia-rapida.md`
- `docs/operacion/troubleshooting.md`
- `docs/operacion/seguridad-privacidad.md`
- `README.md`

Current behavior to replace:

- `CodexSection.review_model` and `CodexState.review_model` are required strings.
- Default configuration forces `o4-mini`.
- `build_codex_review_args` always emits `--model <review_model>`.
- There is no project-level or CLI-level Codex reasoning override.
- `CodexState.session_model` exists but is not populated and must not become a second source of truth for this feature.

The Codex CLI supports optional `--model` and configuration overrides through `-c <key=value>`. The intended explicit reasoning command fragment is equivalent to:

```text
-c
model_reasoning_effort="high"
```

The implementation must pass every argument as a separate subprocess argument and must not use shell interpolation.

## Cursor Rules And Skills

Cursor must follow all repository-local rules, especially:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Use repo-local skills when their descriptions match the work. In particular, use the documentation acceptance/governance skill for documentation changes if it is available to Cursor. No `AGENTS.md` was found when this plan was authored.

## Architecture Guardrails

- The exact prepared `state.codex.session_id` remains the only session identity used for review. Never use `--last` and never start a fresh Codex session.
- Inheritance is represented by omission: no configured model means no `--model`; no configured reasoning effort means no `model_reasoning_effort` override.
- Do not infer, copy, or persist a guessed session model or reasoning effort.
- Explicit model and reasoning overrides are independent. Supplying one must not cause a default value to be injected for the other.
- Prepared run state remains the immutable execution source of truth. Subsequent edits to `ai_dev_loop.yaml` must not alter an already prepared run.
- Existing configs that explicitly contain `review_model` must retain their current behavior.
- Existing persisted runs with a string `review_model` and no reasoning field must remain loadable and resumable.
- Configuration and state schemas must agree with the Pydantic models. Optional YAML fields must be accepted when omitted and when explicitly `null` if the chosen model representation supports both.
- Command arguments must remain a list passed with `shell=False`; do not interpolate TOML, model IDs, session IDs, prompts, or paths through a shell.
- Logs and inspect output may report whether an override is inherited or explicit, but must not expose prompts, session IDs, credentials, or environments.
- Automated tests must use fake executables or pure command construction. Do not consume model tokens.
- WSL installation occurs only after all required validation succeeds.

## Implementation Plan

### 1. Define Optional Configuration Semantics

Update `CodexSection` in `src/ai_dev_loop/config.py`:

- Change `review_model` to `str | None` with a default of `None`.
- Add `review_reasoning_effort: str | None = None`.
- Validate non-empty strings when values are present; reject whitespace-only values.
- Validate reasoning effort against values supported by the installed Codex CLI/config contract. Prefer a shared constant and include at least the documented/current values needed by this project, including `high`. Do not silently normalize unknown values.
- Remove the forced `o4-mini` review model from `default_config_dict`; represent inheritance explicitly with `null` or omission consistently with generated examples.

Update configuration overrides:

- Keep `--codex-review-model` as an optional explicit override.
- Add `--codex-review-reasoning-effort` mapped to `codex.review_reasoning_effort`.
- An absent CLI option must preserve the value from repository/default configuration.
- Do not add a magic string such as `inherit`, `session`, or an empty string to clear values.

Because Typer's optional string cannot distinguish “not supplied” from an explicit null without a separate clearing flag, clearing a repository override through CLI is not required in this phase. Inheritance is configured by omitting or setting the YAML field to `null` before `prepare`.

### 2. Update Project Configuration Schema And Examples

Update `src/ai_dev_loop/schemas/project-config-v1.json`:

- Remove `review_model` from the Codex required-field list.
- Permit `review_model` to be a non-empty string or `null`.
- Add optional `review_reasoning_effort` with the same allowed values enforced by Python, plus `null`.
- Keep `additionalProperties: false`.

Update `tests/fixtures/sample_repo/ai_dev_loop.yaml` and config tests to cover:

- Both fields omitted.
- Both fields explicitly `null`.
- Explicit model only.
- Explicit reasoning only.
- Both explicit.
- Invalid empty/whitespace model.
- Invalid reasoning effort.
- CLI override precedence for each field.

### 3. Persist Prepared Review Selection Without Guessing

Update `CodexState` and run preparation:

- Make `review_model` optional with default `None`.
- Add `review_reasoning_effort: str | None = None`.
- Copy the effective optional values into the run state during `prepare`.
- Leave `session_model` unused unless existing architecture requires it for compatibility; do not populate it by guessing.

Update `src/ai_dev_loop/schemas/run-state-v1.json` compatibly:

- Allow `review_model` to be string or `null`.
- Add optional nullable `review_reasoning_effort`.
- Do not make the new field required, so historical run documents without it remain schema-valid.
- Verify historical state containing a string `review_model` still loads.

If run-state schema validation currently requires `review_model`, it may remain required as a nullable property for newly serialized runs only if historical fixtures prove compatible. Prefer accepting historical documents that omit newly optional fields.

### 4. Build The Codex Resume Command Conditionally

Update `build_codex_review_args` in `src/ai_dev_loop/runners/codex.py`:

- Preserve root option ordering: `codex exec --cd <repo> --sandbox <sandbox> resume ...`.
- Append `--model <value>` only when `review_model` is present.
- Append `-c 'model_reasoning_effort="<value>"'` only when `review_reasoning_effort` is present. Construct the TOML value deterministically and safely as one argument; do not invoke a shell.
- When neither is present, emit neither override and allow the exact session to supply both values.
- Preserve `--json`, `--output-schema`, `--output-last-message`, exact session ID, and stdin prompt marker.

Update `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc` because its current command-shape contract requires `--model`. Document optional model and reasoning fragments without weakening exact-session, structured-output, safety, or artifact guarantees.

### 5. Make Status, Inspect, And Diagnostics Clear

Review current human and JSON output paths for effective/prepared Codex settings.

- Where review model or reasoning is displayed, show a concise value such as `inherited from session` when `None`.
- Keep output backward compatible where practical.
- Ensure no code assumes `review_model` is always a string.
- Ensure errors from an explicitly unsupported model or reasoning effort still point to the configured override and do not incorrectly claim inheritance.

Do not add noisy logs or expose full command payloads beyond existing redacted argument policy.

### 6. Update Documentation

Update all user-facing documentation that currently says `review_model` is required or always forced, including at minimum:

- `README.md`
- `docs/referencia/configuracion.md`
- `docs/guia/configuracion-repositorio.md`
- `docs/guia/guia-rapida.md`
- `docs/operacion/troubleshooting.md`
- `docs/operacion/seguridad-privacidad.md`
- Any CLI reference or phase traceability page affected by the new option.

Documentation must explain:

- Default/recommended inheritance example:

```yaml
codex:
  command: codex
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write
```

- Explicit model and reasoning example:

```yaml
codex:
  command: codex
  review_model: gpt-5.6-terra
  review_reasoning_effort: high
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write
```

- `review_model` omitted/null means the resumed session's model is used.
- `review_reasoning_effort` omitted/null means the resumed session's reasoning configuration is used.
- Overrides are independent and frozen into prepared run state.
- “Normal speed” requires no special Codex override; this feature must not invent or document an unsupported speed setting.
- The exact-session and “do not use the original interactive UI concurrently” safety warning remains in force.
- How to inspect effective configuration before `prepare`.
- How to reinstall an updated local build in WSL.

Do not present Cursor Agent model IDs such as `gpt-5.6-terra-high` as Codex CLI model IDs. For Codex, keep model and reasoning effort separate.

### 7. Automated Tests

Add or update tests at the appropriate levels.

Unit tests:

- Config defaults inherit both values.
- Optional values validate and resolve with correct precedence.
- Prepared state captures `None` and explicit values.
- Command construction emits neither override when both are absent.
- Command construction emits only `--model` for model-only configuration.
- Command construction emits only `model_reasoning_effort` for reasoning-only configuration.
- Command construction emits both in deterministic order when both are present.
- Explicit `high` produces the exact TOML configuration argument expected by Codex CLI.
- `--last` never appears and the exact session ID remains present.
- Redacted command output does not leak prompt content.

Schema/regression tests:

- Project config accepts omitted and nullable values.
- Run state accepts historical documents without `review_reasoning_effort`.
- Existing explicit-model config remains valid.
- Invalid reasoning values fail before any subprocess starts.

Integration tests with fake Codex:

- A review run with inheritance invokes fake `codex exec resume` without `--model` and without reasoning override.
- Explicit overrides arrive at the fake executable as separate argv values.
- Resume/retry uses values persisted at `prepare`, even if repository YAML changes afterward.
- No real Codex or Cursor process is invoked.

Documentation tests:

- MkDocs strict build succeeds.
- Examples validate against the effective project schema where existing tooling supports it.

### 8. Validation Before Installation

Run the repository's full required validation suite:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run python -m pytest -q -s
uv run python -m build
uv run mkdocs build --strict
```

Also run focused CLI/config checks without model activity:

```bash
uv run ai_dev_loop --help
uv run ai_dev_loop prepare --help
uv run ai_dev_loop config validate --repo tests/fixtures/sample_repo
```

Use a temporary fixture repository if the sample fixture cannot be validated directly. Do not run a real review turn merely to prove argument inheritance.

### 9. Install The Verified Build In WSL

After every required validation command passes, install the current repository version into the active WSL user tool environment:

```bash
uv tool install --force .
```

Then verify the installed executable, not only `uv run`:

```bash
which ai_dev_loop
ai_dev_loop --version
ai_dev_loop prepare --help
```

Additionally inspect the installed package behavior with a non-model unit-level command or import to confirm that an inherited Codex configuration builds a resume command without `--model` or `model_reasoning_effort`. Do not start or resume a real user run and do not invoke a real Codex model.

If validation fails, do not install. If installation fails, preserve the source changes and report the exact failure and recovery command; do not uninstall the previously working tool.

## Testing Criteria

The implementation is accepted only when automated evidence proves:

- Repository config can omit both Codex overrides.
- Omitted overrides persist as inheritance semantics in prepared state.
- `codex exec resume` receives no model/reasoning override when both are absent.
- Each override can be supplied independently.
- Explicit model and reasoning values are reproduced exactly in subprocess argv without shell interpolation.
- Existing explicit-model repositories retain current behavior.
- Historical run state without the new reasoning field remains readable.
- The exact Codex session ID, review schema, output artifacts, sandbox, and stdin prompt contracts remain unchanged.
- Full unit/integration tests, static checks, package build, and strict documentation build pass.
- The installed WSL executable reflects the new behavior.

External CLIs must be faked in automated tests. No tests may depend on credentials, network access, model availability, or token-consuming calls.

## Validation

Cursor's final response must include:

- Files changed, grouped by code, schema/tests, rules, and documentation.
- Exact semantics for omitted, null, and explicit model/reasoning fields.
- Commands executed and their pass/fail results.
- The built artifact path.
- The output of `which ai_dev_loop` and `ai_dev_loop --version` after WSL installation.
- Confirmation that no real Cursor/Codex model call and no user run start/resume occurred.
- Any compatibility limitation discovered.

Manually inspect at least these four generated command shapes in test evidence:

```text
# inherit model and reasoning
codex exec ... resume --json ... <session-id> -

# explicit model only
codex exec ... resume --model <model> --json ... <session-id> -

# explicit reasoning only
codex exec ... resume -c model_reasoning_effort="high" --json ... <session-id> -

# explicit model and reasoning
codex exec ... resume --model <model> -c model_reasoning_effort="high" --json ... <session-id> -
```

Exact placement may follow the installed CLI's accepted option ordering, but all options must remain subprocess argv entries and the tests must pin the chosen deterministic order.

## Risks Or Recovery Notes

- Omitting `--model` relies on Codex CLI resume semantics. Pin this contract in fake-command tests and document that the resumed session remains the source of truth; do not inspect private rollout internals to compensate.
- Existing configs may rely on the old default `o4-mini`. This is an intentional default behavior change and must be prominent in documentation and release notes/change summary.
- Old prepared runs with an explicit model must continue to use that model. Do not retroactively reinterpret stored values.
- A repository config changed after `prepare` must not change the active run; prepare a new run to change review selection.
- `-c` values are TOML. Build the exact value safely and cover quoting with tests.
- Installation from the working tree can expose uncommitted source changes to the WSL tool. Install only after validation and report the source path used.
- If `uv tool install --force .` fails, the prior executable may still exist. Verify with `which ai_dev_loop` and `ai_dev_loop --version`; do not delete tool directories manually.

## OpenQuestions

None.
