# Phase 13 - Cursor Usage-Limit Recovery With Auto Fallback

## Goal

Recover a failed Cursor turn caused by Cursor's model usage limit without losing the
existing Cursor chat, partial repository work, the approved prompt, or the audit trail.

When a Cursor execution fails with the narrowly recognized usage-limit condition, an
interactive `ai_dev_loop start` or `resume` must offer to create an immutable recovery
successor that continues the **same Cursor chat** using the Cursor model `auto`. The
successor must send a deterministic continuation prompt that tells Cursor to inspect
and preserve the existing unstaged/untracked work, embeds the exact previous
orchestrator prompt, and resumes the task from that state.

For non-interactive use, no model switch may occur automatically. The user must be
able to perform the same recovery explicitly:

```text
ai_dev_loop recover <failed-run-id> --cursor-model auto
ai_dev_loop resume <recovery-run-id>
```

The motivating incident is:

```text
crypto-sentinel-20260712T205916Z-b2d828
```

It failed while Cursor was executing the initial implementation in chat
`fa1f9a07-...` with `grok-4.5-fast-xhigh`. Cursor had already produced partial,
unstaged and untracked Phase 8 changes before returning `ActionRequiredError` for a
monthly usage limit. The installed CLI advertises `auto`, and its documented command
shape supports `--resume <chat-id>` with `--model <model>`.

That real run is evidence only. Do not recover, resume, alter, stage, or otherwise
mutate it during implementation, tests, installation, or validation.

## Non-Goals

- Do not create a new Cursor chat for fallback recovery.
- Do not change the model of the failed source run in place.
- Do not retry arbitrary Cursor failures, timeouts, authentication failures, malformed
  output, unavailable models, or user aborts with `auto`.
- Do not infer a usage-limit failure from `RunState.last_error` alone.
- Do not silently select `auto` in a non-TTY environment, CI, JSON workflow, or after
  a negative/absent confirmation.
- Do not stage, unstage, discard, reset, clean, stash, commit, or otherwise mutate the
  target repository from `recover`.
- Do not bypass the normal post-Cursor `git add -A`, staging validation, or Codex review
  boundary after the recovered Cursor turn succeeds.
- Do not expose usage/billing details, model provider error prose, prompt text, chat
  IDs, patches, or raw artifacts in default status/log/recovery output.
- Do not perform real Cursor or Codex model calls, tool updates, commits, pushes, or
  releases as validation.

## Scope

- Narrow structured classification of Cursor usage-limit failures.
- Durable failed-turn repository fingerprinting and recovery eligibility.
- A `cursor` recovery checkpoint and immutable successor lineage.
- A frozen Cursor-model override on the successor, with `auto` as the interactive
  fallback choice.
- Deterministic continuation prompts for both initial and correction turns.
- TTY confirmation after `start`/`resume` fails, plus explicit `recover` support for
  non-interactive callers.
- State/schema, manifests, artifact copying, idempotency, status, inspect, list, logs,
  tests, rules, and user documentation.
- Package build and WSL installation after the full validation suite passes.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `archive/implementation-history/plans/phase-5-bounded-review-fix-loop-and-resume.md`
- `archive/implementation-history/plans/phase-10-codex-session-runtime-and-cli-compatibility.md`
- `archive/implementation-history/plans/phase-11-recover-failed-runs.md`
- `archive/implementation-history/plans/phase-12-cursor-index-mutations-and-staging-recovery.md`
- `src/ai_dev_loop/workflow_engine.py`
- `src/ai_dev_loop/runners/cursor.py`
- `src/ai_dev_loop/runners/cursor_output.py`
- `src/ai_dev_loop/iterations.py`
- `src/ai_dev_loop/resume_planner.py`
- `src/ai_dev_loop/recovery_planner.py`
- `src/ai_dev_loop/commands/recover.py`
- `src/ai_dev_loop/cli.py`
- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/schemas/run-state-v1.json`
- Existing fake CLI, workflow, recovery, Phase 10-12, privacy, artifact, and CLI tests.
- User-facing docs for execution, recovery, troubleshooting, configuration, security,
  observability, CLI reference, and phase traceability.

Verify the installed Cursor CLI contract without starting a real agent:

```text
agent --help
agent models
```

At authoring time `agent --help` documents both `--resume [chatId]` and `--model`, and
the current model catalog contains `auto`. The implementation must still probe the
actual installed catalog when resuming the successor; model catalogs are not stable
configuration.

## Cursor Rules And Skills

Cursor must follow all repository-local rules, particularly:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Use any repository-local documentation/acceptance skill that applies. Update the
relevant rules in the same change so this recovery path remains durable.

## Architecture Guardrails

- The source run remains terminal and immutable. Every fallback creates a successor
  run; it never edits source `state.json`, prompt artifacts, Cursor metadata, or Git.
- The successor must preserve the exact `cursor.chat_id` from the source and invoke
  Cursor as `agent -p ... --resume <exact-chat-id> --model <successor-model> ...`.
  `create-chat` is forbidden for this checkpoint.
- The source's Cursor model remains recorded in the source. The successor freezes the
  selected fallback model in its own `cursor.model`; changing YAML after recovery must
  not change it.
- Treat `auto` as a requested Cursor model, not a claim about which provider/model
  Cursor will choose internally. Persist the requested value and retain normal
  stream-json metadata for the resolved model reported by Cursor, if any.
- Classify a usage-limit failure only from the contemporaneously captured Cursor
  process outcome and protected artifacts. Use a dedicated, versioned failure code,
  not a fuzzy search of a human-written `last_error` string.
- The classifier must require a nonzero Cursor exit plus the specific
  `ActionRequiredError` usage-limit/switch-model signal. It must be conservative:
  unknown or partially matching errors remain ordinary failed Cursor executions.
- Store a safe normalized error summary and failure code in state/metadata/events.
  Keep raw stderr only in the existing protected artifact; do not echo billing amount,
  reset date, provider prose, or account details through normal CLI output.
- For an eligible usage-limit failure, capture `after-cursor` status, verify repository
  identity (root, Git dirs, branch, and HEAD), and persist a binary-safe content
  fingerprint before marking the source failure recoverable. The fingerprint covers
  cached changes, tracked worktree changes, and untracked non-ignored paths/content,
  as Phase 12 does for successful Cursor output.
- `recover` must recompute and exactly match the failure fingerprint before creating a
  successor. Reject branch/HEAD/repository changes, tracked/untracked content drift,
  missing required artifacts, unsafe special files, missing/mismatched chat identity,
  prompt/plan drift, or an incomplete/ambiguous failed-turn record.
- Partial unstaged and untracked work is expected for this checkpoint. It is evidence
  to preserve and inspect, not a reason to discard it. It becomes eligible for normal
  `stage_mode` normalization only after the recovered Cursor turn returns success.
- Recovery must reuse the exact prompt body that was sent before the failure:
  `prompts/cursor-initial.txt` for iteration 1, or the exact Codex findings artifact
  for a correction. Do not reconstruct it from logs or summarize findings.
- The continuation envelope must embed that source prompt byte-for-byte and be stored
  as a sensitive successor artifact before Cursor is launched. It must tell Cursor to
  inspect all existing tracked unstaged and untracked non-ignored changes, preserve
  valid partial work, avoid starting over, and continue the task under the new model.
- Preserve Phase 12 correction restrictions: no commit, amend, reset, checkout/switch,
  stash, clean, merge, rebase, tag, push, or repository-identity changes. The
  orchestrator retains final staging ownership.
- A recovery with the same source run, incomplete iteration, verified fingerprint,
  source prompt hash, and requested fallback model is idempotent and reuses its active
  successor. Do not create parallel successors with different fallback models from the
  same failed checkpoint. If that successor later fails, recover that successor to
  preserve a clear lineage chain.
- `recover --dry-run` is read-only with respect to both the target repository and run
  store: no successor, locks that persist, fingerprints, artifact writes, tool updates,
  model calls, or agent invocation.
- `recover` itself never launches Cursor, Codex, or an updater. Only `resume` invokes
  the agent. This remains true when the interactive CLI offers the recovery; it must
  create the successor only after the failed `start`/`resume` lock has been released.
- Existing recovery checkpoints (`staging`, `reviewing`, `process_review`) and legacy
  adoption semantics must retain their current behavior and schema compatibility.

## User Experience Contract

### Interactive `start` and `resume`

When a run fails with the recognized code `cursor_usage_limit`, the original command
must first persist the terminal source failure and release its run lock. In a TTY only,
the CLI then displays a safe message and asks:

```text
Cursor reached the usage limit for its configured model.
Create a recovery successor that continues the same Cursor chat using model `auto`? [y/N]
```

- Default is `No`.
- On `No`, EOF, Ctrl-C, or a non-TTY stdin, leave the source failed and print the
  explicit safe recovery command. Do not create a successor.
- On `Yes`, call the same recovery service used by the explicit command with
  `cursor_model="auto"`, print the successor identity, then call `resume` for that
  successor using the user's existing tool-update policy.
- If the post-recovery compatibility probe rejects `auto` or the user declines a tool
  update, leave the successor resumable/interrupted and print its explicit `resume`
  command. Do not alter the source.
- Do not ask after failures that are not exactly classified as `cursor_usage_limit`.

Avoid calling `recover_run` from inside the workflow's active lock. Model the
post-failure handoff as a typed outcome/error or equivalent signal consumed by the CLI
after `start_run`/`resume_run` has unwound.

### Explicit recovery

Extend the command shape to:

```text
ai_dev_loop recover <failed-run-id> --cursor-model <model> [--dry-run] [--output text|json]
```

- `--cursor-model` is required for the new `cursor` usage-limit checkpoint and invalid
  for all existing recovery checkpoints. The interactive path supplies `auto`.
- Do not make `auto` an implicit default for the command: an explicit argument is the
  user attestation that a model change is intended.
- The command may permit another non-empty catalog model for a future fallback choice;
  it must not mutate the source YAML. `resume` performs the normal live catalog probe
  before execution.
- Dry-run reports the requested fallback model, source model (redacted/safe as current
  output conventions require), checkpoint, prompt/fingerprint verification, and any
  existing matching successor. It does not claim the model is currently available.
- A successful recovery prints a normal `resume_command`; only the interactive
  start/resume convenience path invokes it automatically.

### Continuation prompt

Create a deterministic helper and protected artifact, for example:

```text
prompts/cursor-recovery/01.usage-limit-continuation.txt
```

The envelope must contain a fixed header and the exact prior prompt body. It should
state, without reproducing account/billing details:

```text
This is an ai_dev_loop Cursor recovery turn in the existing chat.

The previous Cursor turn stopped because its configured model reached a usage limit.
Continue this same task using the currently selected model.

Inspect the current repository state before editing. Existing tracked unstaged changes
and untracked non-ignored files are partial work from the interrupted turn. Preserve
and complete valid work; do not discard it or start the implementation over.

Do not commit, amend, reset, checkout/switch branches, stash, clean, merge, rebase,
tag, push, or change repository identity. The orchestrator will normalize staging and
Codex will review the complete staged result after this turn succeeds.

Previous orchestrator prompt follows verbatim:

<exact initial prompt or exact Codex findings prompt>
```

For a correction, use the exact stored findings prompt as the body, not a previous
execution envelope. The recovery header supplies the same staging/Git constraints,
so duplicate/nested correction headers are unnecessary. Preserve original bytes and
newlines after the fixed header.

## Implementation Plan

### 1. Add a narrow Cursor failure classifier

In `src/ai_dev_loop/runners/cursor.py` or a focused adjacent module:

- Add typed Cursor failure classification, at minimum `cursor_usage_limit` and
  `unknown`/no-special-classification.
- Classify only a nonzero process result whose protected stderr or structured error
  events contain the required `ActionRequiredError` and usage-limit/model-switch
  markers. Use carefully tested, case-insensitive predicates; do not match a generic
  phrase such as `limit`.
- Return the classification and a safe summary through `CursorExecutionResult` or a
  dedicated result type. Preserve raw stderr only in existing sensitive files.
- Extend cursor iteration metadata with a stable `failure_code` and safe summary when
  a classified execution fails. Do not place raw provider messages in metadata/events.
- Keep the current stream-json parser behavior and normal success path compatible.

Unit-test positive canonical messages, capitalization/whitespace variants, missing
markers, unrelated `ActionRequiredError`, arbitrary model failures, malformed JSON,
and a nonzero process with no stderr. Assert raw billing/reset details never enter the
safe metadata or event payload.

### 2. Capture durable evidence for an eligible failed Cursor turn

Refactor `_run_cursor_turn` in `src/ai_dev_loop/workflow_engine.py` so artifact capture
is ordered safely for both success and the recognized failure:

1. Persist pre-Cursor status and launch Cursor as today.
2. Persist post-Cursor status and redacted invocation metadata for every completed
   subprocess result, including nonzero exits.
3. On `cursor_usage_limit`, validate repository identity and capture a Phase-12-style,
   binary-safe failure-output fingerprint before transitioning the source to `failed`.
   Store a distinct reason/capture phase in a sensitive artifact, for example
   `git/cursor-output/01.usage-limit-failure.json`; never overwrite a successful
   post-Cursor artifact.
4. Persist the fingerprint path/hash and source prompt path/hash in the iteration
   metadata, then mark the source failed with a safe normalized message and emit a
   safe reason-code event.
5. If identity/fingerprint capture cannot complete, preserve the original execution
   failure but do not make it recoverable through this new path.

Do not capture a recovery fingerprint for unknown failures or timeouts. Timeouts retain
their existing `interrupted` semantics.

### 3. Extend state and schemas for the cursor checkpoint

Update `state.py`, `run-state-v1.json`, state serialization/loading tests, and fixture
schemas together.

- Add `cursor` to recovery checkpoints and `cursor_usage_limit` to recovery reason
  codes.
- Make the source staged-patch hash nullable only where necessary for the new cursor
  checkpoint. Keep it required/non-null and structurally identical for existing
  staging/review checkpoints.
- Add typed, checkpoint-specific recovery fields for:
  - source Cursor model;
  - successor requested fallback Cursor model;
  - source prompt relative path and SHA-256;
  - failure-output fingerprint SHA-256/path or equivalent safe reference;
  - continuation-envelope relative path and SHA-256.
- Enforce: cursor checkpoint requires `cursor_usage_limit`, a non-empty fallback
  model, exact source prompt evidence, and fingerprint evidence; it must reject
  staging-only fields such as `previous_staged_patch_sha256`.
- Enforce conversely that existing checkpoints reject cursor-only fallback fields.
- Preserve readability of historical state that has no new recovery fields.

Prefer a dedicated typed submodel if it makes these mutually exclusive contracts clear;
do not add an unvalidated bag of optional dict fields.

### 4. Plan and validate usage-limit recovery

Extend `recovery_planner.py` with a `cursor` checkpoint analysis before the current
`cursor_turn_complete` rejection:

- Locate the latest incomplete Cursor iteration and require its protected metadata to
  prove nonzero exit plus `failure_code == "cursor_usage_limit"`.
- Verify exact `cursor.chat_id` agreement between state and `cursor/chat.json`.
- Validate repository identity, plan contract, prompt contract, timeout contract, and
  the source prompt artifact/hash.
- Load the recorded usage-limit fingerprint and recompute the current content
  fingerprint; reject any status/content drift, special file, or unreadable artifact.
- Allow initial implementation and correction iterations. For corrections also require
  the previous review/fix-prompt/staged-patch checkpoint that normally authorizes the
  correction.
- Set `checkpoint="cursor"`, `reason_code="cursor_usage_limit"`, and an explicit
  current action of Cursor continuation. Do not resolve or mutate Codex runtime merely
  because this recovery will begin with Cursor; preserve frozen Codex state unchanged.
- Extend safe/idempotent successor discovery to include the checkpoint, incomplete
  iteration, failure fingerprint, source prompt hash, and requested fallback model.
  Refuse a different active fallback successor for the same source checkpoint rather
  than creating a parallel one.

Do not parse `last_error` as eligibility evidence. The state error is diagnostic only.

### 5. Create a cursor-checkpoint successor

Extend `commands/recover.py` and its lower-level helpers:

- Add `cursor_model: str | None` to the service signature and CLI option
  `--cursor-model`.
- Reject a supplied model when analysis identifies an existing checkpoint, and reject
  omission for a usage-limit cursor checkpoint with a clear validation error.
- Create a successor with the source chat ID unchanged, the requested fallback model in
  `successor.cursor.model`, the same Cursor command/output/sandbox/permissions, and
  the same frozen Codex runtime/session.
- Copy only the artifacts required to establish lineage: source plan/prompt snapshots,
  chat identity, failure iteration artifacts/evidence, prior completed reviews and
  staged patches, and correction prompts where applicable. Do not copy raw failure
  content into normal output.
- Write the deterministic continuation envelope into the successor before the first
  resume, record it in typed recovery/iteration metadata and the manifest, and protect
  file permissions as for other prompt artifacts.
- Set the successor to the existing resumable checkpoint shape so `resume` plans a
  Cursor turn for the same incomplete iteration. Update `read_cursor_prompt`/
  `cursor_prompt_path` or introduce a narrowly scoped recovery prompt resolver so this
  first resumed turn uses the continuation envelope rather than the raw initial/fix
  prompt.
- Make retry behavior deterministic: a later interruption of that successor reuses the
  same continuation artifact, while a later usage-limit failure creates the next
  successor in the explicit lineage chain.
- Update recovery result and analysis rendering with safe `cursor` checkpoint labels,
  fallback model, continuation status, and the standard `resume_command`. Do not
  render full chat IDs, prompt bodies, or raw model errors.

### 6. Add the interactive post-failure convenience flow

Keep recovery policy separate from tool-update policy. Introduce a small typed
post-Cursor-failure handoff/policy at the command boundary rather than embedding Typer
prompts in workflow or recovery services.

- Make `start_run` and `resume_run` surface enough typed information for their CLI
  callers to identify a just-persisted `cursor_usage_limit` source failure without
  rereading untrusted prose.
- In `cli.py`, after the workflow has returned/raised and released its locks, if stdin
  is a TTY and the failure code is exactly `cursor_usage_limit`, show the fixed prompt
  from the User Experience Contract and call `typer.confirm(..., default=False)`.
- On approval, call `recover_run(run_id, cursor_model="auto")`, then `resume_run` for
  the returned successor using the same `ToolCompatibilityPolicy` selected for the
  original command. Render a clear recovery handoff followed by normal resume output.
- On rejection, no TTY, EOF, or Ctrl-C, leave the source terminal and print the exact
  explicit recovery command including `--cursor-model auto`.
- Ensure `--output json` does not cause an interactive prompt or mixed human/JSON
  output. If start/resume currently lack `--output`, retain their current output
  contract and document their TTY behavior; do not invent a half-JSON path.
- Never let a convenience-flow failure overwrite the original source error. If
  recovery or successor resume fails, report the new run ID and its next safe action.

### 7. Preserve observability, privacy, and command contracts

Update status/inspect/list/log renderers and event conventions:

- `status` for an eligible usage-limit source must state the safe reason and recommend
  `recover <run-id> --cursor-model auto`; it must not print account/billing details.
- `inspect` shows recovery lineage, source and fallback model identifiers according to
  existing redaction conventions, continuation artifact classification, and verified
  checkpoint, but not its contents.
- `recover --dry-run --output json` exposes reason/checkpoint/fallback request and
  safe verification booleans/relative classifications only.
- Events/logs use stable codes such as `cursor_usage_limit_detected`,
  `cursor_usage_limit_recovery_offered`, `..._declined`, `..._successor_created`, and
  `..._continuation_started`. Do not record prompt text, full chat IDs, raw stderr, or
  billing/reset data.
- Update manifest/artifact discovery so copied evidence and continuation envelopes are
  discoverable, remain sensitive, and are included in integrity checks.

### 8. Tests

Add focused unit and integration coverage using fake CLIs and temporary repositories.
Do not touch the motivating real run.

Required tests include:

1. Cursor runner recognizes only the canonical usage-limit failure and redacts safe
   metadata/events.
2. Initial and correction failures persist post-Cursor status, identity evidence, and
   binary-safe fingerprints before becoming recoverable.
3. A usage-limit source with partial tracked unstaged and untracked changes is eligible
   only when its current fingerprint matches exactly.
4. Content change with identical path/status, changed index, changed branch/HEAD,
   missing prompt/fingerprint/metadata, special file, chat mismatch, and ordinary
   Cursor failure are rejected.
5. `recover --dry-run --cursor-model auto` is read-only; actual recovery changes only
   the run store, never Git or source artifacts.
6. Recovery rejects omitted fallback model for the cursor checkpoint and rejects the
   option for staging/review checkpoints.
7. The successor preserves exact chat ID, freezes `cursor.model == "auto"`, preserves
   Codex runtime, carries complete lineage, and resumes the same incomplete iteration.
8. The fallback invocation contains the exact `--resume <source-chat-id>` and
   `--model auto`, never calls `create-chat`, and sends the stored continuation
   envelope.
9. Initial recovery envelope embeds `cursor-initial.txt` byte-for-byte; correction
   recovery embeds the exact findings file byte-for-byte, avoids nested envelopes, and
   instructs safe treatment of existing partial work.
10. After recovered Cursor succeeds, normal `git add -A`, staged snapshot capture, and
    Codex review occur; Cursor is not rerun before the requested continuation.
11. Repeated equivalent recovery reuses the active successor; a different requested
    model cannot create a parallel successor; a terminal successor requires explicit
    recovery of that successor.
12. TTY accept creates then resumes the successor only after source lock release; TTY
    decline, EOF/Ctrl-C, and non-TTY leave the source failed and print a safe explicit
    command. Existing tool-update prompts still work on the successor.
13. Existing Phase 11/12 recovery, legacy staging adoption, aborted runs, Codex review
    recovery, and ordinary Cursor-failure behavior remain unchanged.
14. Pydantic models, JSON schema requiredness/nullability, `model_dump`, persisted
    states, and historical fixtures remain aligned.

## Documentation Changes

Update the relevant Spanish documentation and README without exposing sensitive
artifacts:

- `README.md`: one concise note on usage-limit fallback recovery.
- `docs/operacion/prepare-start-resume-abort.md`: interactive prompt, same-chat model
  switch, source/successor lifecycle, and normal post-success staging.
- `docs/operacion/troubleshooting.md`: exact usage-limit symptom, why `resume` cannot
  resume the failed source directly, explicit recovery with `--cursor-model auto`,
  partial-work safety, and cases where recovery must refuse.
- `docs/referencia/cli.md`: `recover --cursor-model`, TTY/non-TTY behavior, dry-run,
  and no automatic model switch in automation.
- `docs/operacion/seguridad-privacidad.md`: protected raw stderr, safe reason codes,
  fingerprint validation, no source mutation, and no billing details in normal output.
- `docs/operacion/observabilidad.md`: artifacts/events/status/inspect behavior for the
  cursor checkpoint.
- `docs/referencia/configuracion.md`: clarify that fallback model is frozen in a
  recovery successor rather than editing `ai_dev_loop.yaml`; `auto` is requested
  routing, not a pinned underlying provider model.
- `docs/referencia/trazabilidad-fases.md`: add this phase and links to recovery usage.
- `docs/guia/guia-rapida.md` and `docs/guia/flujo-handoff.md`: same-chat continuity,
  user confirmation, and the instruction to avoid manually editing partial work before
  recovery unless intentionally abandoning it.

## Validation And Delivery

Run the repository's established checks, at minimum:

```text
uv run ruff format --check
uv run ruff check
uv run mypy src
uv run pytest -q -s
uv run python -m build
uv run mkdocs build --strict
```

Also run focused tests for Cursor runner classification, workflow failure capture,
recovery planner/service/CLI, schema/state compatibility, and fake end-to-end recovery.

Verify help and a fake CLI catalog after installation:

```text
uv run ai_dev_loop recover --help
uv tool install --force .
ai_dev_loop recover --help
agent models
```

Report:

- files changed grouped by failure classification, workflow evidence, recovery/state,
  CLI, tests, rules, and docs;
- the exact recovery contract and non-goals;
- focused and full validation results;
- package build artifacts and installed WSL `ai_dev_loop` path/version;
- confirmation that no real Cursor/Codex model calls and no real run recovery/resume
  occurred during validation.

## Acceptance Criteria

- A narrowly proven Cursor usage-limit failure can be recovered into an immutable
  successor with the same Cursor chat and an explicitly selected fallback model.
- `start`/`resume` offer `auto` only interactively and only after the source failure is
  durable and unlocked; non-interactive callers require explicit recovery.
- The continuation turn sees an exact previous prompt plus deterministic instructions
  to inspect/preserve partial unstaged/untracked work and continue rather than restart.
- The successor invokes Cursor with the exact stored chat ID and fallback model, and
  normal staging/Codex review resumes only after Cursor succeeds.
- Any repository/prompt/session/fingerprint drift, ambiguous failure, or ordinary
  Cursor failure is rejected safely before a successor exists.
- Existing recovery behavior remains compatible, source runs stay immutable, sensitive
  data remains protected/redacted, and all validation passes.
