# Phase 14 — Remote Controller and Isolated Codex Review Session

## Goal

Allow a user who is operating Codex Desktop remotely (including from the ChatGPT
mobile app) to prepare, launch, monitor, and abort an `ai_dev_loop` run without
opening a WSL terminal or leaving the original planning conversation.

The design must keep two distinct Codex sessions:

- **Controller session (A):** the original planning conversation. It remains available
  for read-only run status and explicit control actions.
- **Reviewer session (B):** a same-directory Codex fork created after the plan and
  Cursor prompt are approved. It prepares the run and then remains inactive. Every
  automated review resumes only this exact session.

The user-facing result is:

```text
A: approve plan and prompt
  -> fork same directory to B
  -> B: prepare with controller_session_id=A and reviewer_session_id=B
  -> A: launch the prepared run in a detached local worker
  -> A: "¿cómo va el run?" / abort / inspect safe next action
  -> worker: Cursor -> staging -> codex exec resume B
```

## Non-Goals

- Do not let the Python orchestrator create, clone, or otherwise synthesize Codex
  conversation forks. Forking is a Codex app action performed by the installed skill
  through the app capability available to that Codex task.
- Do not resume, write to, or send any prompt to the controller session A from the
  worker.
- Do not permit the reviewer session B to run `start`, `launch`, `resume`, `abort`,
  or status-control actions after it has prepared the run.
- Do not replace the exact-session review requirement with a new `codex exec` session,
  `--last`, inferred IDs, or subagents.
- Do not add cloud workers, remote execution services, a web server, a Windows daemon,
  or a dependency on `systemd`.
- Do not alter Cursor chat identity, review-result semantics, Git staging ownership,
  recovery semantics, model probing, or tool-update policy.
- Do not commit, push, tag, reset, clean, stash, unstage, or otherwise rewrite Git
  state.

## Scope

- Add durable controller/reviewer identity metadata to new prepared runs.
- Add a detached local launch path and read-only controller-status lookup.
- Add an installed `ai-dev-loop-controller` skill, and update the installed
  `ai-dev-loop-handoff` skill for the A/B workflow.
- Refactor the integration installer/status/uninstaller to manage more than one
  package-owned skill while preserving existing users and unrelated files.
- Update current CLI wording, integration contracts, product documentation, schemas,
  and automated tests.

## Out of Scope

- Target-repository `ai_dev_loop.yaml` changes.
- Target-repository review skills and the configured `codex.review_skill`.
- Changes to the Desktop/WSL rollout bridge topology, Desktop hook trust, Codex
  SQLite, `CODEX_HOME`, authentication, or model availability.
- Automatic push notifications from the worker to ChatGPT/Codex. Phase 14 supports
  on-demand status requested from A; a future phase may add an explicit notification
  integration after choosing an authorized delivery mechanism.
- Migration of existing runs. Existing runs remain readable and use the current
  `start`/`resume` workflow; only new A/B-prepared runs gain controller operations.

## Required Context

Read before implementation:

- `archive/implementation-history/master-plan.md`
- `docs/guia/flujo-handoff.md`
- `docs/operacion/prepare-start-resume-abort.md`
- `docs/integraciones/codex-desktop-wsl.md`
- `src/ai_dev_loop/commands/prepare.py`
- `src/ai_dev_loop/commands/status.py`
- `src/ai_dev_loop/run_discovery.py`
- `src/ai_dev_loop/workflow_engine.py`
- `src/ai_dev_loop/abort_control.py`
- `src/ai_dev_loop/process.py`
- `src/ai_dev_loop/state.py`
- `src/ai_dev_loop/schemas/run-state-v1.json`
- `src/ai_dev_loop/integrations/codex/{assets.py,paths.py,install.py,session_start.py}`
- `src/ai_dev_loop/integrations/codex/skill/SKILL.md`
- integration, workflow, abort, state-schema, and CLI tests.

Confirm from the current Codex task environment that the app exposes an authorized
same-directory fork capability and an authorized way for B to send the prepared
run identity back to A. Treat those as app-level actions, not CLI calls. Do not invent
an undocumented Desktop IPC protocol or scrape Codex rollout files to infer a parent
thread relationship.

## Cursor Rules And Skills

Follow all repository-local rules, especially:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Use the repository skill `ai-dev-loop-docs-acceptance-governance` for the documentation
and acceptance portions of this phase. The local `create-cursor-plan` and
`review-staged-changes` skills are planning/review aids, not runtime dependencies.

## Architecture Guardrails

- **Identity ownership:** persist the exact controller session ID only for control
  lookup, and preserve `state.codex.session_id` as the exact reviewer B session ID
  used by every `codex exec resume`. Never derive either ID from `--last`, history
  scanning, rollout content, or an untrusted request.
- **Reviewer quiescence:** launch is allowed only when the persisted controller ID and
  reviewer ID are valid, exact, and different. The skill must leave B inactive before
  A launches. The CLI can validate identity inequality but cannot prove Desktop UI
  activity; its output must state that B must remain untouched while the run is active.
- **State contract:** add a typed optional controller section to `RunState` and the
  versioned run-state schema. Historical run states without it stay readable. Do not
  overload `CodexState`, recovery lineage, `result`, or `last_error`.
- **Local worker ownership:** `launch` starts one detached local child with argument
  arrays and `shell=False`. Persist a dedicated, atomic, sensitive launcher record
  containing safe process identity data (run ID, PID/PGID, start timestamp, parent
  PID, redacted argv, and log paths). Verify PID identity conservatively before
  reporting it live; stale or ambiguous records must not authorize signalling.
- **Existing workflow ownership:** the worker invokes the existing `start_run` path.
  It owns normal run/repository locks and existing active Cursor/Codex child metadata.
  `status` remains read-only while it runs. `abort` continues to persist an abort
  request and lets the worker/active child honor it; no controller action may reset,
  unstage, or kill an ambiguous process.
- **Controller lookup:** a read-only command resolves runs by exact controller session
  ID and repository root. If zero or multiple non-terminal candidates match, return a
  safe, actionable result; never choose the latest run implicitly. Permit an explicit
  run ID only to disambiguate.
- **Privacy:** full controller and reviewer IDs, raw skill-to-skill messages, prompts,
  reviews, patches, and process output remain sensitive. Default controller status
  shows only the existing safe state summary, shortened identities, phase/iteration,
  worker liveness, last safe error, and next safe action.
- **Integration assets:** ship package resources for both skills. The installer must
  manage only its own `SKILL.md` files at their declared skill directories, preserve
  user-added files, validate `hooks.json` before any deletion, and retain the existing
  SessionStart hook/bridge safety model.
- **No fake app bridge:** skills may use only Codex app capabilities surfaced to the
  current task. If the fork or cross-thread message capability is absent, stop with a
  bounded explanation; do not fall back to a guessed session ID or an external shell
  workaround.

## Implementation Plan

### 1. Define the new A/B state and CLI contracts

1. Add a small typed optional `ControllerState` (or equivalently named explicit model)
   to `state.py`, with exact `controller_session_id`, a schema version if needed, and
   only durable fields required for controller lookup. Keep reviewer identity in the
   existing `CodexState.session_id`.
2. Add strict UUID validation for `--controller-session-id`, reusing the narrow
   validation rules used for Codex session IDs. Reject equality with the reviewer ID.
3. Extend `PrepareOptions`, `prepare`, `PrepareResult`, human rendering, and JSON
   output with an optional `--controller-session-id`. For A/B handoff, make it
   required by the handoff skill; retain ordinary prepare compatibility when omitted.
   Do not report `requires_codex_exit: true` for a valid A/B run. Instead emit an
   explicit machine-readable launch eligibility/requirement stating that **the
   reviewer session** must be inactive and a different controller session may launch.
4. Update `run-state-v1.json`, serialization/schema alignment tests, manifests when
   applicable, and all state fixtures. Preserve old `state.json` compatibility through
   an optional field; do not silently assign a controller to historical runs.
5. Update status next-action text: legacy runs keep their current warning, whereas an
   A/B prepared run directs the controller skill to launch only after B is inactive.

### 2. Build controller discovery and read-only status

1. Add a focused command/service, for example `ai_dev_loop controller status`, that
   accepts exact `--controller-session-id`, `--repo-path`, optional `--run-id`, and
   `--output text|json`.
2. Use `run_discovery` plus canonical repository identity to select only runs whose
   persisted controller ID exactly matches. Default to non-terminal runs. Fail safely
   on zero/multiple candidates rather than selecting by timestamp. An explicit run ID
   must still match both the controller and repository.
3. Compose its response from existing `status` data plus launcher liveness. Never read
   prompt/review artifacts, call Cursor/Codex, acquire mutation locks, or change run
   state.
4. Add a stable JSON response for the controller skill: run identity, status,
   current/max review iteration, safe active component/worker information, safe last
   error/result, and next safe action. Shorten all session IDs in human output.

### 3. Implement detached `launch` and lifecycle records

1. Add `ai_dev_loop launch <run-id> --controller-session-id <exact-id>` rather than
   changing the semantics of `start`. Validate run state, controller identity,
   reviewer/controller inequality, terminal status, and repository identity before
   spawning; delegate normal full preflight to the worker's existing `start_run`.
2. Start a detached Python worker with `start_new_session=True`, direct its safe
   stdout/stderr to dedicated sensitive run artifacts, and persist launcher metadata
   before returning success. Do not use `nohup`, shell command strings, `systemd`, or
   any target-repository path for state/logs.
3. Make launch idempotent: if a live, identity-verified worker already owns the same
   non-terminal run, return its safe status instead of spawning another worker. If a
   record is stale, preserve it for diagnostics, clear only when conservatively safe,
   and let normal locks prevent duplicate mutation.
4. Ensure worker completion atomically records terminal launcher outcome without
   replacing the workflow's canonical run result/error. Keep worker exceptions
   redacted and route actionable diagnostics to protected artifacts.
5. Integrate with abort/status/inspect/logs only to report safe worker state. Abort
   must not blindly signal a stale worker; it must keep the existing durable abort
   request and active-child process-control contracts intact.
6. Keep `start` and `resume` supported for existing local workflows. Do not make
   `launch` implicitly install tools, relax model compatibility, or alter TTY policy.

### 4. Package the two global Codex skills

1. Refactor the integration asset layout/API from one handoff skill to a declared set
   of package-owned skill descriptors. Each descriptor has a stable directory name,
   resource loader, package-content comparison, installation path, and uninstall
   identity.
2. Keep `ai-dev-loop-handoff` but update its instructions to orchestrate the A/B flow:
   create a same-directory app fork after explicit plan approval; let B obtain its
   SessionStart context; run `prepare --controller-session-id <A>` from B; return the
   prepared run identity to A through an authorized app message; then leave B
   untouched. It must stop if that context/capability is unavailable.
3. Add `ai-dev-loop-controller`. It is controller-A-only and handles natural prompts
   such as “¿cómo va el run?”, “lanza el run”, and “aborta el run”. It must use the
   current exact SessionStart ID, call only `controller status`, `launch`, `abort`, or
   existing read-only inspection commands, summarize safely in Spanish, and refuse to
   act if A equals the run's reviewer B.
4. Do not create a second status hook. Keep the existing SessionStart hook as the sole
   minimal identity provider. Update its additional context only if needed to explain
   controller/reviewer roles without retaining parent/fork relationships or transcript
   content.
5. Update integration installation, uninstall, status, doctor, result structures, and
   JSON output for multiple skills. Preserve existing singular status fields for
   backwards compatibility where practical, and add a stable per-skill collection.
   Update the global-integrations rule so the two documented paths are the only
   package-managed skills.

### 5. Documentation and safety wording

1. Update the handoff, prepare/start/resume, Codex Desktop + WSL, troubleshooting,
   integration-install, security/privacy, observability, reference configuration, and
   phase-traceability pages to describe only verified Phase 14 behavior.
2. Document the A/B lifecycle, reviewer quiescence requirement, detached-worker
   behavior, on-demand controller status, run ambiguity handling, abort semantics, and
   recovery after worker/interruption failures.
3. Explain that the installer adds both skills and the existing SessionStart hook; it
   does not auto-trust hooks, create the Desktop bridge, open a new Codex session, or
   send background mobile notifications.
4. Do not expose full IDs, prompts, reviews, patches, raw command output, or arbitrary
   process environments in docs examples or default command output.

## Testing Criteria

Automated tests are required because this phase changes CLI behavior, persisted state,
subprocess lifecycle, global installation, and safety controls. Use fake `agent` and
fake `codex` executables only; never invoke real models.

- **Unit — state/schema/prepare:** valid A/B metadata persists; invalid, malformed,
  equal, or missing controller IDs are handled according to the compatibility
  contract; old states remain readable; model/schema serialization stays aligned.
- **Unit — discovery/controller status:** exact controller/repository matching;
  zero/multiple candidates; explicit run mismatch; terminal/non-terminal selection;
  short/redacted human output; JSON stability; pure read-only behavior while locks are
  held.
- **Unit/integration — launch worker:** argv-only spawn, protected artifacts and
  permissions, success/failure outcome recording, idempotent live-worker response,
  stale/ambiguous worker safety, no duplicate worker, no target-repository state
  writes, and no raw process output in summaries.
- **Integration — workflow/abort:** fake worker reaches normal Cursor/staging/Codex
  flow with B's exact session ID; controller A is never passed to the Codex runner;
  abort request reaches a detached run safely; status sees active worker/child;
  cancellation/crash leaves a recoverable diagnostic without unsafe cleanup.
- **Integration — installer:** first install creates both skill files; second install
  is idempotent; package contents are verified independently; upgrade changes only
  owned files; uninstall removes both owned `SKILL.md` files but preserves user-added
  files and unrelated skills/hooks; invalid `hooks.json` prevents deletion of either
  skill or hook script.
- **Skill content regression:** assert frontmatter names and key guardrail text:
  exact IDs, no `--last`, B must be quiescent, no start from B, controller status is
  read-only, and controller actions do not expose sensitive artifacts.
- **Documentation/build:** `mkdocs build --strict` succeeds and documentation does
  not claim automatic notifications or an unimplemented app API.

## Validation

Run, in this order when available:

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

Also run focused tests for state/schema, prepare, controller/launch, abort, and global
integration paths before the full suite. Do not run real `ai_dev_loop integrations
install` against the user's home, open `/hooks`, fork a real conversation, or start a
real Cursor/Codex run as validation. A manual remote Desktop/mobile acceptance check
is a separate, explicit user-approved follow-up after the package passes automated
validation.

## Risks Or Recovery Notes

- A background worker can outlive the initiating Codex tool call. Treat its durable
  launcher record, normal locks, and abort request as the source of truth; never infer
  liveness from an old PID alone.
- A controller/reviewer mismatch is safety-critical. Refuse the action, preserve
  artifacts, and require the exact identity rather than guessing or choosing a recent
  session.
- Codex app fork/message capabilities are an external integration boundary. If the
  capability is unavailable in a particular surface, the skills must stop safely and
  explain the missing capability; the worker must not substitute another Codex session.
- Existing failed/interrupted runs retain existing recovery behavior. If the detached
  worker fails after meaningful workflow progress, use the established `recover` and
  `resume` paths only where their current eligibility checks permit it.
- Installer changes affect user-global files. Complete parsing/validation of
  `hooks.json` and all target resolution before deleting or replacing any owned asset.

## OpenQuestions

None. The plan deliberately confines Codex app forking and cross-thread messaging to
capabilities surfaced to the executing Codex task; absence of either is a defined
safe-stop condition, not an implementation ambiguity.
