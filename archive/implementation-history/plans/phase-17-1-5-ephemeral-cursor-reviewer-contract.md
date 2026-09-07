# Phase 17.1.5 — Fresh Codex reviewer bootstrap and scheduler alignment

## Goal

Replace the local A/B handoff's dependency on a pre-existing, forked Codex
session B with one **fresh Codex CLI reviewer B per durable run**. Controller A
prepares the run and passes the review model and reasoning effort as required
command parameters. Those values become immutable run inputs. At the first
review boundary, the worker creates B with a new `codex exec` invocation,
captures its exact newly-created session identity, and persists it. All later
review passes in that same run resume only that identity.

B is review-only: it reads the approved plan and staged changes, returns the
existing schema-validated review decision, and never dispatches Cursor,
implements a fix, or changes Git state. This removes the active-writer conflict
caused by trying to resume a Codex Desktop-forked B from a detached worker.

The phase changes the **current A-controlled local workflow** so it can be used
to implement and review Phase 17.2 and later. It also makes the Phase 17 central
scheduler adopt the identical one-B-per-run contract when its review effect is
implemented in Phase 17.5. Phase 17.1 must first be manually validated and
committed; this phase is committed on top of that accepted baseline.

## Non-Goals

- Do not re-run, recover, alter, or delete the historical failed A/B runs.
- Do not launch a real Cursor or Codex review as implementation validation.
- Do not implement the scheduler tick, systemd backend, Cursor effects, or the
  Phase 17.5 scheduler review loop in this phase.
- Do not create a B during `prepare`, `scheduler submit`, or `scheduler start`.
  A run has no B identity until its first review boundary.
- Do not create a replacement B after a persisted B exists, after an ambiguous
  fresh creation, or merely because a review invocation fails.
- Do not let B dispatch work, stage, commit, push, reset, clean, stash,
  unstage, or otherwise mutate the target repository.
- Do not change PR-review v2's session contract or its public behavior.

## Scope

- Change the legacy controller-A `prepare` path so it requires A's exact
  controller identity plus `--codex-review-model` and
  `--codex-review-reasoning-effort`, but no B session ID. Validate these command
  parameters and freeze the selected values into the durable run state and
  immutable artifacts. They must not come from YAML, a pre-existing B runtime,
  a session capture, or a later CLI default.
- Replace the previous handoff flow: A no longer forks or messages B. A prepares
  and launches the durable run directly; the worker lazily bootstraps B at the
  first review. Update package-owned Codex handoff/controller instructions and
  their tests accordingly, without installing any user-global skill.
- Refactor the Codex review adapter into two explicit modes: initial fresh
  bootstrap and subsequent exact resume. The initial argv uses new `codex exec`
  with the frozen `--model`, `-c model_reasoning_effort=...`,
  `--sandbox read-only`, working directory, structured-output schema, and
  bounded artifact paths. Subsequent reviews use the exact captured session ID
  and the same frozen model/reasoning settings. Neither mode may use `--last`,
  a fork, or an ephemeral session.
- Persist only a validated identity emitted by the fresh Codex CLI structured
  event contract, atomically with the first review attempt's durable evidence.
  The adapter must reject missing, malformed, conflicting, or ambiguous session
  identity. It must never infer an ID from logs, a Desktop/WSL rollout, SQLite,
  or an unrelated active session.
- Make recovery exact: if B is durably bound, resume it; if first creation or
  identity capture is ambiguous, block with a redacted safe action and preserve
  evidence. A recovery path must never start a second B for that run.
- Replace Phase 17 scheduler submission's pre-existing B-session binding with
  the same frozen review model/reasoning binding. `scheduler submit` receives
  A plus both explicit reviewer parameters, starts no agents, and preserves no
  B session until Phase 17.5's first review effect. Update the master and child
  plans to make this shared contract explicit.
- Update factual CLI, state/artifact, security, troubleshooting, and workflow
  documentation, the relevant repository rules, schemas, configuration models,
  test fixtures, and integration assets. Any control-plane update requires a
  manual acceptance review in addition to fake-backed automated tests.

## Out of Scope

- The direct, non-controller legacy `prepare` interface remains unchanged until
  the Phase 17.7 cutover. It must not silently adopt a different reviewer
  identity contract.
- A scheduler-state importer or automatic conversion of an existing
  session-bound submitted run into a fresh-B run.
- User-global skill installation, hook-trust changes, Codex Desktop/WSL bridge
  edits, systemd enablement, or target-repository mutation.
- Any automatic model selection, reasoning-effort fallback, tool update, or
  creation of a reviewer from mutable `ai_dev_loop.yaml` at review time.

## Required Context

Read before editing:

- `AGENTS.md`, the Phase 17 master, Phase 17.1, Phase 17.1.75, and Phases
  17.2–17.7. Phase 17.1 is historical once manually accepted; do not rewrite
  its staged contract retroactively.
- All eight `.cursor/rules/*.mdc`, especially the codex-review, loop/resume,
  orchestrator, state/schema, governance, and global-integration contracts.
- `commands/{prepare,launch,controller,scheduler}.py`, `cli.py`, `state.py`,
  `launcher.py`, `launch_worker.py`, `workflow_engine.py`, `review_runtime.py`,
  `runners/codex.py`, `review_result.py`, `response_schema.py`, and the
  prepare/launch/recover/controller integration tests.
- `scheduler/{domain,application,infrastructure}/`, scheduler schemas and
  migrations, `tests/unit/scheduler/`, and
  `tests/integration/test_phase17_1_submit.py`.
- `src/ai_dev_loop/integrations/codex/{assets.py,skill/SKILL.md,controller_skill/SKILL.md}`
  and `tests/unit/test_codex_integration_assets.py`.
- `ai_dev_loop.yaml`, `docs/referencia/{cli,configuracion}.md`,
  `docs/operacion/{estado-artefactos,seguridad-privacidad,troubleshooting}.md`,
  and `/home/rojobad/Projects/celatex360-platform/.codex/agents/cursor-supervisor.toml`
  as a structural reference only. The reviewer provider is Codex CLI, not
  Cursor CLI.
- The installed `codex exec --help` and a fake executable characterization
  fixture. Verify the machine-readable new-session event used for identity
  capture before wiring a real command; do not rely on undocumented transcript
  text.

## Cursor Rules And Skills

Follow all eight repository rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`

Also follow `AGENTS.md` and
`.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md` for factual
documentation and manual-acceptance material. The staged-review skills govern
the independent review after implementation and must not be invoked by the
implementation agent.

The fresh-B rule supersedes the pre-existing/forked B language only for the
controller-A local workflow and the Phase 17 scheduler. Preserve PR-review v2
and the explicitly excluded direct legacy mode until an approved cutover says
otherwise.

## Architecture Guardrails

- **One reviewer identity:** each durable run creates at most one B. Before the
  first review, B is absent. Fresh bootstrap captures one exact ID; every later
  review and recovery resumes that ID. No `--last`, fork, session rollout read,
  Desktop writer, or second new reviewer is a valid substitute.
- **Immutable reviewer configuration:** the model and reasoning effort are
  required command inputs when A prepares/submits the run. Their normalized
  values are hashed into the immutable context/idempotency identity and passed
  unchanged to every B invocation. The worker never rereads, defaults, or
  upgrades them from YAML or a runtime session.
- **Safe CLI boundary:** use argv arrays, explicit CWD, bounded stdin/stdout/
  stderr artifacts, `--sandbox read-only`, `--json`, and the review result
  schema. Do not use `shell=True`, a writable reviewer sandbox, a raw prompt in
  metadata, or an unbounded stream parser.
- **Structured authority:** only the existing versioned, schema- and
  cross-field-validated review result drives findings, test status, residual
  risk, and the immutable Codex-authored correction prompt. Stream events and
  prose are audit evidence; they do not decide workflow state.
- **Atomic persistence and recovery:** bind B identity only after strict event
  validation and durable artifact hashing. A crash or conflicting identity
  before that point is an uncertainty blocker, not permission to retry fresh.
  Persisted B identity remains protected/redacted from status output.
- **No Git authority expansion:** preflight remains an admission check. After
  admission, reviewer/implementer agents own the normal reviewed exchange; do
  not add broad continuous baseline or Git-status policing. The scheduler and
  worker retain reservation, artifact-integrity, exact identity, and explicit
  review-decision boundaries.
- **No agent at submission:** `prepare`, `scheduler submit`, and scheduler
  `start` only validate/freeze durable inputs. They must not probe a model, read
  B runtime state, spawn Codex/Cursor, stage, or mutate Git.
- **Compatibility:** never silently reinterpret an existing session-bound local
  or central run. It remains read-only/status-inspectable and execution-blocked
  with an explicit fresh prepare/re-submit safe action; never patch state/SQLite
  by hand or convert it automatically.

## Implementation Plan

1. Characterize the current controller-A handoff, `prepare` validation,
   `launch_worker` review path, Codex argv/result parsing, state schema, and
   scheduler v1 submitted context with tests before moving behavior. Add a fake
   `codex` fixture whose JSONL stream includes the supported new-session event;
   make the parser contract explicit and fail closed on any other shape.
2. Add typed, versioned `FreshCodexReviewerBinding`/equivalent state to local
   and central contexts. It contains normalized frozen model and reasoning
   effort, no B ID until bootstrap, then the protected exact B identity and
   bootstrap evidence. Retire B-session/runtime capture only from the affected
   A-controlled local and scheduler paths; retain required compatibility models
   elsewhere until cutover.
3. Change A's local `prepare` command and the package-owned handoff/controller
   instructions: require A identity plus the two reviewer CLI parameters;
   remove the B fork/message/session argument from this path; keep `launch` as
   A's explicit action. Update CLI help, validation, redacted state projection,
   idempotency/hash construction, and all compatibility diagnostics.
4. Change `scheduler submit` to require and freeze the same two reviewer
   parameters with A identity, without a B session ID or runtime lookup. Add a
   transactional schema migration/version-refusal strategy for Phase 17.1
   ledgers. It must preserve existing rows/artifacts exactly and make
   read-only/status inspection plus execution refusal explicit for incompatible
   submitted runs; the safe action is fresh re-submission, never conversion.
5. Split the Codex adapter into fresh-bootstrap and exact-resume commands. The
   fresh command invokes new `codex exec` with frozen model/effort,
   `--sandbox read-only`, JSON and output schema; it validates and persists the
   fresh ID. The resume command uses only that stored ID, preserves command and
   result-schema contracts, and rejects any inconsistent output identity.
6. Refactor worker/recovery state transitions so the first review creates B
   once, while correction reviews reuse it. Treat partial launch, duplicate
   bootstrap event, missing identity, malformed output, timeout, and process
   interruption as blocked/uncertain with preserved artifacts. Do not create a
   fresh retry B.
7. Update the Phase 17 master and child plans: 17.2–17.4 carry the frozen
   binding but launch no B; 17.5 performs fresh bootstrap then exact resumes;
   17.6 recovery preserves the one-B invariant; 17.7 removes the legacy
   fork-based handoff at cutover. Update rules and package assets so no active
   instruction asks A to supply, fork, or message a pre-existing B.
8. Update active docs and operational material to distinguish: A explicitly
   selects model/effort at prepare/submit; no reviewer starts then; the first
   review creates one read-only Codex B; later reviews resume it; ambiguity
   blocks. Require manual review for configuration, rule, and installed-skill
   control-plane changes.

## Testing Criteria

- **Unit/schema:** validate required model/reasoning command inputs, strict
  enum/value normalization, no YAML/runtime fallback, redaction, immutable
  context hashing/idempotency, state transitions from no-B to bound-B, and
  local/central migration or version-refusal behavior.
- **Codex adapter contract:** fake Codex verifies new-bootstrap argv includes
  `exec`, read-only sandbox, exact frozen model/effort, JSON/output schema, and
  no `resume`, `fork`, `--last`, or ephemeral option. Its structured creation
  event is captured exactly once. Later passes verify `exec resume <stored-id>`
  with the same frozen values and never create a second B.
- **Failure/recovery:** cover absent/malformed/conflicting creation event,
  bootstrap crash before/after durable persistence, nonzero exit, timeout,
  partial output, stale/duplicate completion, and recovery. Every uncertain
  bootstrap case preserves evidence and blocks; none may launch a new B.
- **Workflow/integration:** fake local A preparation/launch proves no B is
  forked or contacted, no agent runs at prepare, first review creates B, a
  correction review resumes it, and B never dispatches implementation. Fake
  scheduler submission proves the same frozen binding with no subprocess,
  runtime lookup, Git mutation, or reviewer creation.
- **Compatibility/control plane:** PR-review v2 stays unchanged. Tests make
  the selected direct-legacy compatibility behavior explicit. Package assets,
  rules, CLI/config/docs references, and integration tests contain no stale
  pre-existing-B instruction. Do not run real Codex, Cursor, systemd, model
  APIs, user-global skill installation, or target-repository mutation.

## Validation

Run focused suites, then:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/unit/test_codex_runner.py \
  tests/unit/test_response_schema.py \
  tests/unit/test_codex_integration_assets.py \
  tests/unit/test_controller_launch.py \
  tests/unit/scheduler \
  tests/integration/test_launch_worker.py \
  tests/integration/test_phase17_1_submit.py \
  tests/integration/test_phase17_1_5_*.py
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
git diff --check
```

Run existing legacy and PR-review regressions affected by changed shared models.
Automated validation uses only fake executables and temporary XDG/HOME roots.
A separately authorized manual acceptance may test one disposable, read-only
Codex B after the fake suite passes; it must not be treated as a substitute for
the schema, recovery, and control-plane tests.

## Risks Or Recovery Notes

The observed active-writer failure came from resuming a Desktop-forked B in a
detached process. A new CLI B removes that shared writer, but only if creation
identity is captured deterministically and no retry manufactures a replacement
reviewer. Losing that invariant would split review history and invalidate the
review/fix dialogue.

Phase 17.1's accepted commit, staged patch evidence, and historical run data
remain independently recoverable. This phase changes future A-controlled runs
and new scheduler submissions only. Existing session-bound contexts must remain
untouched until the explicit compatibility decision is implemented safely.

## OpenQuestions

None. The controller-A path is the current workflow changed by this phase; the
direct legacy path remains unchanged until Phase 17.7. Existing session-bound
contexts remain read-only/status-inspectable and execution-blocked, with fresh
preparation or submission as the non-destructive safe action.
