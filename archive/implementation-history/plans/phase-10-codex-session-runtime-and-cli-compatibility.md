# Phase 10 - Codex Session Runtime And CLI Compatibility

## Goal

Make Codex review execution deterministic and safer by capturing the exact effective model and reasoning effort from the approved Codex session during `prepare`, persisting only those safe metadata fields, and passing them explicitly to every `codex exec resume` review turn unless the user configured an explicit override.

Add preflight compatibility checks for the installed Codex and Cursor CLIs. When a required model is not supported by the installed CLI, interactive `start`/`resume` may offer to run the CLI's official self-updater with explicit user consent. Non-interactive execution must never block for input or update software implicitly.

Document the observed cross-family compaction risk when resuming between GPT-5.5 and GPT-5.6 and recommend keeping a run on the session's original model family.

## Non-Goals

- Do not persist, print, summarize, hash, or copy Codex transcript content while extracting session runtime metadata.
- Do not infer model or reasoning from `~/.codex/config.toml` when session metadata is available.
- Do not silently fall back to Codex CLI defaults when inheritance was requested.
- Do not promise that every GPT-5.5/GPT-5.6 switch always compacts; document the validated behavior and operational risk accurately.
- Do not update Codex Desktop or Cursor Desktop on Windows.
- Do not update `ai_dev_loop` through the new external-tool update flow.
- Do not use undocumented vendor download URLs, `curl | sh`, package-manager guesses, or shell interpolation for updates.
- Do not implement a generic package manager or semver-based “latest release” service.
- Do not run real model turns in automated tests.

## Scope

- Safe discovery and parsing of Codex rollout metadata for exact session IDs.
- Native WSL sessions and Codex Desktop sessions reachable through the existing WSL bridge.
- Configuration precedence and prepared run-state semantics for explicit versus session-derived review runtime values.
- Codex command construction using the effective captured model and reasoning effort.
- Codex and Cursor version/capability probes before agent execution.
- Interactive consent and non-interactive flags for running official CLI self-updaters.
- State, schemas, logs, metadata, status/inspect output, tests, Cursor rules, and MkDocs documentation affected by these behaviors.
- Reinstallation of the validated `ai_dev_loop` build into WSL after implementation.

## Out of Scope

- Target application code in repositories controlled by `ai_dev_loop`.
- Changing Cursor chat model inheritance; Cursor continues using the explicitly prepared `cursor.model`.
- Automatic operating-system, Node, Python, `uv`, Git, Codex Desktop, or Cursor Desktop upgrades.
- Background update daemons, scheduled update checks, release feeds, or telemetry.
- Recovery or automatic reuse of terminal `failed` runs from previous phases.
- Commits, pushes, tags, releases, or package-index publication.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `archive/implementation-history/plans/phase-4-codex-review-runner.md`
- `archive/implementation-history/plans/phase-7-5-codex-desktop-wsl-bridge.md`
- `archive/implementation-history/plans/phase-9-optional-codex-review-model-and-reasoning.md`
- `src/ai_dev_loop/commands/prepare.py`
- `src/ai_dev_loop/cli.py`
- `src/ai_dev_loop/workflow_engine.py`
- `src/ai_dev_loop/runners/probes.py`
- `src/ai_dev_loop/runners/codex.py`
- `src/ai_dev_loop/process.py`
- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/config.py`
- `src/ai_dev_loop/integrations/codex/desktop_bridge.py`
- `src/ai_dev_loop/schemas/project-config-v1.json`
- `src/ai_dev_loop/schemas/run-state-v1.json`
- Relevant unit/integration fake CLI fixtures under `tests/`
- Current user-facing installation, configuration, handoff, troubleshooting, safety, CLI, and state/artifact documentation.

Validated incident evidence that motivates this phase:

- A Desktop-originated session was recorded with `gpt-5.6-sol` and reasoning `high`.
- Phase 9 omitted `--model` and `model_reasoning_effort` as designed.
- WSL Codex CLI resumed using its local default `gpt-5.5`, not the session model.
- Codex emitted a model mismatch warning and then attempted `pre-sampling compact` with `gpt-5.6-sol`.
- Codex CLI `0.142.5` failed because that model required a newer CLI.
- The rollout contains structured `thread_settings_applied` and `turn_context` events with model/reasoning metadata.
- `agent update` and `codex update` exist locally; Cursor also documents its self-updater. Neither CLI exposes a documented, stable “check only for latest version” command.

## Cursor Rules And Skills

Cursor must follow every repository-local rule, especially:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Use repo-local skills when their descriptions match the task, including documentation acceptance/governance for documentation changes. No `AGENTS.md` was present when this plan was authored.

## Architecture Guardrails

- The exact `codex.session_id` supplied to `prepare` remains the only session identity. Never use `--last`, fuzzy matching, newest-session selection, transcript text, or cwd as a replacement identity.
- Session lookup must use trusted session roots already recognized by the WSL/Desktop bridge architecture. Resolve and validate paths conservatively, including intended bridge symlinks; reject ambiguous duplicate session IDs or unexpected roots.
- Parse rollout files as JSONL. Inspect only allowlisted event types and allowlisted scalar fields required for runtime selection. Never log entire rollout lines or payloads.
- The parser may read lines to locate metadata but must not retain transcript messages, tool content, instructions, prompts, outputs, or arbitrary payload dictionaries.
- Select the latest valid effective session settings before `prepare`: prefer the latest applicable structured turn/settings event, with a documented deterministic precedence among supported event types.
- Persist only safe runtime metadata: model, reasoning effort, optional service tier if actually needed, origin/source classification, and non-sensitive extraction diagnostics. Do not persist the rollout absolute path or transcript content in target repositories.
- Explicit `codex.review_model` and `codex.review_reasoning_effort` remain independent overrides. For each omitted/null field, use the corresponding captured session value.
- If an omitted field cannot be captured safely and unambiguously, `prepare` must fail before creating a usable run. Do not silently use WSL `config.toml` defaults.
- Freeze both configured and session-derived effective values into prepared state. Later session or YAML changes must not alter the run.
- Every automated review must pass the effective model and reasoning explicitly so Codex CLI defaults cannot change reviewer runtime.
- Existing Phase 9 runs remain readable. Add an explicit compatibility/read strategy rather than silently reinterpreting stored `null` as captured values.
- Update prompts are user-interface concerns. Core workflow functions must receive an explicit update decision/policy and must never call `input()` or Typer directly.
- Repository configuration must not be able to authorize software updates. A target repo must never set “auto update” through `ai_dev_loop.yaml`.
- CLI updates mutate the user's WSL environment and require explicit CLI consent. Never derive consent from repository files.
- Invoke only the configured executable plus its official `update` subcommand as argv with `shell=False`. Never use `sudo`, shell commands, pipes, installers, or arbitrary output as executable input.
- Update checks and updates must happen before launching Cursor/Codex model processes and before irreversible workflow progress. Preserve run locks and prevent concurrent updater execution.
- After any update attempt, re-resolve executable paths, capture versions, and rerun executable/auth/model compatibility probes.
- Distinguish “incompatible with required model”, “update may help”, “compatible”, and “unknown”. Do not claim a newer release exists unless there is authoritative evidence.
- In non-TTY execution, never prompt. Incompatibility must fail with actionable commands unless the user supplied an explicit update or override flag.
- Tests must use fake CLIs and sanitized synthetic rollout fixtures. No credentials, network access, real updates, or model tokens.

## Implementation Plan

### 1. Add A Safe Codex Session Runtime Reader

Create a focused module under the existing Codex integration boundary, for example `integrations/codex/session_runtime.py`.

Define typed results such as:

```text
CodexSessionRuntime
  session_id
  model
  reasoning_effort
  origin
  source_event_type
  source_timestamp
```

Requirements:

- Validate the session ID as the exact UUID format already used by the product.
- Search native WSL session storage and the installed Desktop bridge using deterministic roots.
- Match rollout filenames by exact session ID first, then verify the structured `session_meta` ID inside the file.
- Reject zero matches with an actionable bridge/session error.
- Reject multiple distinct rollout matches for the same session ID unless they resolve to the same file.
- Stream the JSONL file line by line with bounded line-size handling and clear malformed-JSON errors.
- Allowlist metadata event types needed by known Codex formats, initially including structured `thread_settings_applied` and `turn_context` forms proven by sanitized fixtures.
- Extract only string scalar fields for model and reasoning/effort. Do not preserve whole payloads.
- Choose the latest complete effective pair deterministically. If model and reasoning arrive in separate events, track only the latest allowlisted scalar values and document the ordering rule.
- Validate extracted model as non-empty and reasoning against the Codex effort values supported by the project. Expand the shared effort set if current Codex supports additional valid values and update schemas/docs consistently.
- Return extraction metadata suitable for audit without transcript content.
- Ensure errors never include raw rollout lines.

Add sanitized fixtures representing:

- Native WSL session.
- Desktop bridge session.
- Settings event followed by turn context.
- Model/reasoning changed mid-session; latest effective settings win.
- Missing reasoning.
- Missing model.
- Malformed JSON.
- Duplicate/ambiguous session files.
- Session ID mismatch between filename and `session_meta`.
- Transcript-shaped events containing sensitive sentinel strings that must never appear in results, errors, logs, or state.

### 2. Resolve Effective Review Runtime During `prepare`

During `prepare`, after validating the exact session ID and before writing a usable prepared run:

1. Read session runtime metadata safely.
2. Resolve each field independently:
   - explicit `review_model` wins;
   - otherwise captured session model;
   - explicit `review_reasoning_effort` wins;
   - otherwise captured session reasoning.
3. Fail safely if an inherited field is unavailable.
4. Warn when an explicit model differs from the captured session model.
5. Emit a stronger operational warning when the switch crosses GPT-5.5 and GPT-5.6 families.

Update persisted state so it distinguishes provenance without ambiguity. Prefer explicit fields such as:

```text
session_model
session_reasoning_effort
review_model
review_reasoning_effort
review_model_source        # session | explicit
review_reasoning_source    # session | explicit
```

Here `review_model` and `review_reasoning_effort` should hold the effective values actually passed to Codex, not `null`, for newly prepared Phase 10 runs. Keep schema/model compatibility for historical Phase 9 states where those fields may be `null`.

Do not copy absolute rollout paths into source/effective config snapshots or target repos. If a run artifact needs extraction evidence, store a safe relative artifact containing only allowlisted metadata.

Update `prepare` text/JSON output, `status`, and `inspect` to show:

- captured session model/reasoning;
- effective review model/reasoning;
- provenance (`session` or `explicit`);
- any model-family mismatch warning.

Never show full session IDs in default human output.

### 3. Pass Captured Values Explicitly To Codex

Update Codex review command construction:

- New Phase 10 runs must always emit `--model <effective-model>`.
- New Phase 10 runs must emit `-c model_reasoning_effort="<effective-effort>"` when the captured/effective reasoning is present.
- Preserve exact session ID, sandbox, JSONL events, output schema, output-last-message, stdin prompt, and option ordering.
- Historical runs with explicit Phase 9 values retain their behavior.
- Historical Phase 9 runs with both values `null` must not be silently claimed as session-derived. Either preserve old no-override behavior with a clear legacy warning or require a newly prepared run; choose the safer behavior and test/document it.

Update Codex review metadata to record safe effective runtime and provenance. Keep transcript and prompts out of metadata.

### 4. Add Cross-Family Compaction Warnings

Add a pure model-family classifier sufficient for warnings, not model routing.

- Recognize GPT-5.5 and GPT-5.6 family identifiers conservatively.
- If an explicit override crosses between those families relative to the captured session model, warn during `prepare` and expose the warning in prepared output/state inspection.
- Do not block valid explicit overrides solely because of family mismatch.
- Do not claim universal compaction behavior.

Use documentation wording equivalent to:

> Reanudar una sesion GPT-5.5 con GPT-5.6, o una GPT-5.6 con GPT-5.5, puede forzar compactacion previa por diferencias de contexto y restricciones entre familias. En las versiones validadas se observo `pre-sampling compact`. Evita cambiar de familia dentro del mismo run y usa el modelo original de la sesion.

### 5. Add Version And Model Compatibility Probes

Extend probes with typed, independently testable results:

- Cursor version from `agent --version` or configured equivalent.
- Cursor configured-model compatibility from `agent models` (retain current behavior).
- Codex version from `codex --version`.
- Codex required-model compatibility using `codex debug models` where supported.
- Prefer a refreshed model catalog for start/resume compatibility, with bounded timeout and a documented fallback to `--bundled`/unknown when refresh is unavailable.
- Parse JSON structurally and tolerate documented catalog shape variants without scanning arbitrary text for success.

Classify each tool:

```text
compatible
incompatible_model
unknown
probe_failed
```

An unrecognized required model is evidence of incompatibility/unknown capability, not definitive proof that a newer release exists. Messages may say “updating may add support,” not “an update is definitely available.”

Store safe preflight artifacts containing command name, installed version, required model, classification, timestamps, and redacted diagnostics. Do not store auth payloads, model catalogs in full, account data, or environments.

### 6. Add Safe Update Consent To `start` And `resume`

Add mutually exclusive CLI controls to both commands, with final names chosen consistently and documented, for example:

```text
--update-tools
--skip-tool-update
--allow-incompatible-tools
```

Required behavior:

- Default interactive TTY behavior:
  - run compatibility probes;
  - if compatible, continue without prompting;
  - if a tool is incompatible, show tool/version/required-model and ask whether to run that tool's official updater;
  - require a separate clear decision for Codex and Cursor when both are affected;
  - default answers to no;
  - after no, fail by default for confirmed incompatibility; continuing requires explicit confirmation or `--allow-incompatible-tools`.
- `--update-tools` is explicit consent to run official updaters for incompatible tools without prompting.
- `--skip-tool-update` never updates; incompatible tools fail with actionable commands unless `--allow-incompatible-tools` is also explicitly permitted by the final CLI design.
- Non-TTY behavior never prompts. Without explicit `--update-tools`, fail on incompatibility and print exact manual commands.
- Reject contradictory flag combinations before state mutation.
- Library-level `start_run`/`resume_run` receive an explicit policy/callback/result; they do not read stdin.

Update execution requirements:

- Cursor: `[configured_cursor_command, "update"]`.
- Codex: `[configured_codex_command, "update"]`.
- Use `shell=False`, bounded updater timeouts, separate stdout/stderr capture, redaction, and no inherited prompt text.
- Do not prepend `sudo` or use a package manager.
- Prevent concurrent updater execution with a conservative XDG-managed update lock or equivalent process-safe mechanism, while respecting existing run locks.
- If an updater fails, preserve current run status/checkpoint appropriately and report artifact paths.
- After success, re-resolve path/version and rerun auth/model compatibility probes.
- Continue only if required compatibility passes, unless the user explicitly allowed incompatibility.
- A successful updater that leaves the model unsupported must report that distinction.

Do not offer updates merely because version strings differ from a hardcoded “latest” constant. The system has no authoritative check-only release source in this phase.

### 7. Place Preflight At Safe Workflow Boundaries

Refactor start/resume ordering so tool compatibility and any approved update happen:

- after run identity/status and lock safety are established;
- before Cursor/Codex child processes;
- before the run is left in a transient status that an updater prompt could strand;
- on `resume` whenever another Cursor or Codex invocation may occur, because tools may have changed since `prepare` or the prior checkpoint.

If compatibility failure occurs after a state transition, follow existing failure-persistence contracts: terminal/recoverable status, `last_error`, structured event, and safe next action must agree. Prefer arranging checks so a declined update does not unnecessarily destroy resumability.

Abort requests must win over updater prompts and updater launches. Never start an update after abort is observed.

### 8. Update Rules, Schemas, And Documentation

Update all affected Cursor rules and schemas in the same change.

Documentation must cover:

- Real session-derived model/reasoning behavior and precedence.
- Safe metadata extraction and privacy boundaries.
- Exact difference between “captured session runtime,” “explicit override,” and “Codex CLI default.”
- GPT-5.5/GPT-5.6 compaction warning using non-universal wording.
- Version and compatibility probes.
- Interactive update consent and non-TTY flags.
- `agent update` and `codex update` manual recovery commands.
- Cursor's default auto-update behavior without assuming it always succeeds.
- Updates affect WSL CLI binaries only, not desktop apps.
- Offline behavior and unknown compatibility.
- Re-preparing runs after session-runtime/config changes.
- Troubleshooting the exact errors:
  - session recorded with one model but resumed with another;
  - `Failed to run pre-sampling compact`;
  - model requires a newer Codex CLI.

Update at minimum:

- `README.md`
- `docs/guia/guia-rapida.md`
- `docs/guia/configuracion-repositorio.md`
- `docs/guia/flujo-handoff.md`
- `docs/guia/instalacion.md`
- `docs/operacion/troubleshooting.md`
- `docs/operacion/seguridad-privacidad.md`
- `docs/operacion/estado-artefactos.md`
- `docs/referencia/cli.md`
- `docs/referencia/configuracion.md`
- `docs/referencia/codex-desktop-wsl-sessions.md`
- `docs/referencia/trazabilidad-fases.md`

### 9. Automated Tests

Unit tests:

- Exact session lookup in native and bridged roots.
- Structured allowlist parsing and latest-settings selection.
- Missing, malformed, duplicate, mismatched, and ambiguous session metadata failures.
- Sensitive transcript sentinels never enter returned metadata or errors.
- Independent explicit/session-derived precedence.
- Effective values and provenance serialize/deserialize consistently.
- GPT-5.5/GPT-5.6 warning classification without universal claims.
- Codex/Cursor version parsing and model-catalog compatibility classification.
- Updater argv uses configured executable and `shell=False` path.
- Contradictory flags fail before mutation.
- No prompt path when stdin is non-TTY.

Integration tests with fake CLIs:

- `prepare` captures session model/reasoning and freezes them.
- Review argv explicitly contains captured model/reasoning.
- Explicit model only and reasoning only override independently.
- Later rollout/YAML changes do not affect prepared state.
- Compatible tools proceed without prompt/update.
- Incompatible Codex prompts in TTY simulation; yes runs fake `codex update`, reprobes, then proceeds.
- Incompatible Cursor follows the equivalent flow.
- Both incompatible require independent decisions and deterministic ordering.
- Declined update fails safely with actionable output.
- `--update-tools` works without prompt.
- Non-TTY default never blocks and never updates implicitly.
- Updater failure, timeout, unchanged version, and still-incompatible-after-update preserve diagnostics.
- Abort before update prevents updater launch.
- Historical Phase 9 state remains readable and follows the documented compatibility path.

Privacy regression tests:

- Prompts, transcript messages, tool output, auth data, full model catalogs, and full session IDs do not appear in default status/logs/errors/events/update metadata.
- Session runtime extraction artifacts contain only allowlisted fields.

Use fake `agent` and `codex` binaries. Do not invoke real self-updaters, networks, credentials, model catalogs, or model turns in automated tests.

### 10. Validation And WSL Installation

Run:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q -s
uv run python -m build
uv run mkdocs build --strict
uv run ai_dev_loop --help
uv run ai_dev_loop prepare --help
uv run ai_dev_loop start --help
uv run ai_dev_loop resume --help
uv run ai_dev_loop doctor
```

Validate only with sanitized fixture sessions and fake CLIs. Do not run `agent update`, `codex update`, a real model review, or a real user run during implementation validation.

After all checks pass, install the verified current package into WSL:

```bash
uv tool install --force .
which ai_dev_loop
ai_dev_loop --version
ai_dev_loop start --help
ai_dev_loop resume --help
```

The WSL installation step updates only `ai_dev_loop`; it must not trigger the new external CLI updater flow.

## Testing Criteria

Acceptance requires automated evidence that:

- `prepare` captures the exact latest session model/reasoning from native and bridged sessions without retaining transcript content.
- Omitted/null review fields resolve to captured session values, not WSL CLI defaults.
- Explicit overrides remain independent and auditable.
- New review commands always pass effective model/reasoning explicitly.
- Model-family mismatch warnings are accurate and non-universal.
- Required model compatibility is checked for both tools before model execution.
- Interactive update consent is explicit, defaults to no, and reruns probes after update.
- Non-interactive execution never prompts or updates without an explicit flag.
- Target repository YAML cannot authorize software updates.
- Update subprocesses use configured binaries, argv lists, bounded timeouts, safe locks, and redacted artifacts.
- Historical run compatibility is intentional and tested.
- Full tests, static checks, build, strict docs, and installed-tool smoke checks pass.

## Validation

Cursor's final response must report:

- Files changed, grouped by session runtime, workflow/update handling, state/schemas, tests, rules, and docs.
- Exact session metadata precedence and event allowlist.
- Historical Phase 9 behavior.
- Interactive and non-interactive update matrices.
- Commands and pass/fail results.
- Confirmation that no transcript content was persisted or printed.
- Confirmation that no real updater or model call ran during tests.
- Installed `ai_dev_loop` path/version after final WSL installation.
- Any catalog/version case classified as `unknown` and why.

## Risks Or Recovery Notes

- Codex rollout schemas can evolve. Fail closed on unknown shapes and add sanitized fixtures for supported variants; never broaden parsing to arbitrary transcript payloads.
- The latest runtime settings may change within a session. Deterministic last-valid-event selection is required.
- Model catalog absence can mean old CLI, entitlement, offline refresh, or rollout state. Report incompatibility/unknown precisely and present update as a possible remedy.
- Self-updaters mutate user-level binaries and may replace files while another run is active. Use process-safe locking and re-probe after updates.
- An updater may succeed while leaving the required model unsupported. Do not equate exit code zero with compatibility.
- Cross-family compaction behavior may change across Codex releases. Document the observed validated behavior and recommendation, not a permanent guarantee.
- Existing failed runs remain terminal. Preserve staged repository work and use a new prepared run or documented manual recovery path.
- If WSL installation fails, preserve the previous executable and source changes; do not delete tool directories manually.

## OpenQuestions

None.
