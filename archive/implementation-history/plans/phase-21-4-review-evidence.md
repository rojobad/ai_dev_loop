# Phase 21.4 — Reviewer Inspection and Exact Prompt Evidence

Status: implementation plan for approval; no execution authorized by this file.
Target local Integration API: `1.3`.

## Goals

Preserve the exact final reviewer stdin before each new scheduler review
attempt, and expose per-attempt review metadata, validated response/findings,
Markdown, correction prompt, and prompt bytes without confusing retries.

## Non-Goals

Do not reconstruct historical prompts, parse Markdown into invented structured
findings, expose a session transcript/hidden-reasoning API, change reviewer
decisions, or alter bootstrap/resume/retry/recovery semantics.

## Scope

New protected prompt/evidence artifacts in the scheduler Codex runner; typed
evidence schema and pure readers; review index/detail/content commands; tests
and API/CLI/privacy/artifact documentation. Changes to
`scheduler/codex_attempt_runner.py` and `scheduler/domain/codex_contract.py`
are permitted for this evidence boundary. No new ledger migration is needed.

## Out of Scope

- Remote monorepo, Bridge runtime/registration/WSS, web/backend/database/cache,
  remote operation execution, deployment, and machine/user management.
- `ai_dev_loop.yaml`, `.agents/skills/`, `.cursor/rules/`, installed global skills,
  hooks/session bridges, real systemd/lingering setup, package reinstallation.
- Scheduler reducers, Git checkpoint authority, recovery policy, automatic
  retries, finalization/commit/push/PR behavior, and retired legacy run import.
  Narrow integration query helpers and explicitly named evidence/probe changes
  below are allowed; do not refactor adjacent runtime behavior.

## Required Context

Planning baseline inspected: commit `d1d8960` (2026-09-18), after Phase 20.9 and
the stricter planning/review guidance. Read current source/tests again once at
admission; do not require this exact HEAD or add continuous baseline gates.
Read `docs/referencia/cli.md`, `docs/operacion/seguridad-privacidad.md`,
`docs/operacion/estado-artefactos.md`, `docs/operacion/observabilidad.md`, and
`pyproject.toml`. Current code/tests establish prerequisites; archived plans
alone do not. The source discussion is consolidated in
`archive/implementation-history/plans/phase-21-local-integration-overview.md`;
this execution plan is self-contained and requires no Downloads file.

Prerequisites: independently accepted Phase 21.1–21.3. Verify their actual CLI,
DTOs, pure artifact readers and fixtures before dependent edits; these phases
are planned at baseline, not evidence of existing capabilities.

- `scheduler/codex_attempt_runner.py::_run_codex_review` builds prompt, adds
  REVIEW_RETRY_OPERATIONAL_ENVELOPE, and sends it directly to run_process_streaming.
  It already stores per-attempt result/report/metadata but not this stdin.
- `scheduler/domain/codex_contract.py` defines per-attempt review paths;
  `scheduler/application/codex_evidence.py` validates outcomes and result hashes;
  `codex_workflow_service.py` owns outcome ingestion, identity and retry policy.
- `runners/codex.py::build_review_wrapper_prompt` includes frozen initial prompt
  and latest Cursor response. Freeze its final output, not its ingredients.
- `review_result.py` and `schemas/codex-review-result-v1.json`: actual result has
  findings_count/highest_severity/review_markdown, not a findings object array.
- `tests/unit/scheduler/test_phase17_5_codex_corrections.py` exercises the real
  runner boundary; Phase 20.6/20.8 tests cover existing review recovery.

## Cursor Rules And Skills

Read `AGENTS.md` and these repository-local Cursor rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`

There is no `.cursor/skills/` directory. Existing rules are sufficient; do not
create a new governance framework for this feature. This plan follows
`.agents/skills/create-cursor-plan/SKILL.md`; independent review follows
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`. Read their contract
coverage and correction-reporting requirements, but do not modify those skills
or invoke the handoff/controller workflow as part of implementation.

`AGENTS.md`, the approved phase, and current scheduler code/tests govern the
supported flow. Legacy rule references to Phase 4/10 session setup, continuous
baseline gates, or Phase 8-only documentation restrictions do not authorize
restoring retired workflows. Preserve current fresh reviewer B bootstrap and
exact subsequent resume. New inspection commands intentionally expose selected
sensitive artifacts; existing default status/history/log privacy stays intact.

## Architecture Guardrails

- The local scheduler ledger, frozen definitions, validated lineage, and protected
  artifacts remain authoritative. Integration DTOs are public projections, never
  aliases for internal Pydantic models or database rows.
- Add the singular `integration` CLI namespace. Preserve the existing plural
  `integrations` installation namespace and all existing scheduler CLI output.
  Put presentation under `commands/integration.py`, public DTOs/application
  readers under a focused `integration_api/` package, and storage queries/path
  handling behind the existing infrastructure boundary. No HTTP server or daemon.
- Integration reads use `SqliteSchedulerStore.open_readonly` and read
  transactions; they never bootstrap/migrate a database, repair state, tick,
  acquire a scheduler write reservation, or execute Git. Read-only means no
  application writes/chmod/mkdir in ledger or artifact paths; ordinary OS access
  metadata and SQLite-managed read coordination are not a new product gate.
- Existing `ProtectedArtifactStore.run_root`, `sequence_root`, and readers that
  call them may create/chmod directories. Do not use those mutating entry points
  from integration reads. Reuse safe lexical path and hash validation through
  pure read helpers; resolve only registered IDs and allowlisted artifact kinds,
  never arbitrary client paths or shell strings.
- Full run/sequence/attempt IDs are public resource keys. They are not full agent
  session IDs. Do not add surrogate ID storage. Summaries exclude prompts, review
  bodies, output, raw argv, environments, credentials, and provider prose.
  Explicit content reads return the captured artifact; do not log their bodies,
  reconstruct missing history, or promise secret removal from arbitrary content.
- No new roles, local tokens, remote authentication, caches, telemetry database,
  continuous baseline checks, arbitrary file browser, or multi-account registry.
  The local process runs under the operator's OS user; remote authentication is
  the future Bridge/control-plane responsibility.
- Same API major is compatible, different major requires UPDATE_REQUIRED at the
  consumer and no further requests. Minor changes are additive. Unknown fields
  are ignored by consumers; capabilities only enable additive features. Never
  add per-minor adapters or treat minor differences as incompatibility.
- Preserve repository reservations, identities, abort/retry semantics, and the
  final staged boundary. Phase 21 authorizes no additional Git effects. Never
  execute agent output. Do not stage/commit/push or run real scheduler controls
  during this implementation; an independently authorized outer scheduler owns
  any staging/checkpoints in its existing workflow.
- Use fake CLIs, temporary native-Linux XDG roots, disposable repositories, and
  deterministic clocks/synchronization. No real models, provider accounts,
  workstation timer/lingering, credentials, live installation, or network in tests.
  Temporary isolated wheel installs for package validation are allowed.
- Each additive phase updates current-version info/schema test expectations,
  while retaining previous producer fixtures and consumer compatibility tests
  unchanged. Historical fixture preservation does not freeze the live version
  or capability values asserted by current CLI tests.

## Implementation Plan

1. After final prompt construction and operational retry prefix, before any
   Codex child launch, publish immutable sensitive files:

   ```text
   codex/reviews/<NN>.<attempt_id>.prompt.txt
   codex/reviews/<NN>.<attempt_id>.prompt-evidence.json
   ```

   Evidence v1 fields: schema_version=1, run_id, attempt_id, review_iteration,
   prompt_path (relative deterministic path), prompt_sha256 (full lowercase
   hex), prompt_size_bytes. All required/non-null, strict integer iteration>=1
   and bytes>=1, no numeric-string/bool coercion. Closed private JSON schema at
   `src/ai_dev_loop/schemas/scheduler-review-prompt-evidence-v1.json` and typed
   model agree on complete representative payloads. Freeze exact UTF-8 bytes
   without newline insertion/redaction/rewriting, with permissions 0600 and
   private directories. Record by deterministic path; no extra SQL table.
2. Use bounded atomic publication with existing write-or-verify semantics in
   this authorized writer. Evidence publication follows complete prompt
   publication, and launch follows verification of both. Do not impose a new
   reviewer-prompt size policy: publish the exact already-constructed byte
   string using its measured length as the writer's max_bytes bound. Cursor
   final text is not currently guaranteed to be bounded by the parser's events
   constant; do not mistake that constant for a launch admission limit. Read
   verification streams the registered byte count/hash without whole-file
   allocation, and transport chunks remain bounded. Identity comes
   from authenticated invocation evidence, not consumer input.
3. Persistence/effect contract:

   | Point | Durable authority/effect/postcondition | Interruption/replay/abort action |
   |---|---|---|
   | Authenticated attempt admitted, child not started | Existing attempt/dispatch ownership; write prompt then evidence | Failure produces existing bounded attempt failure, never launches an unrecorded prompt |
   | Prompt exists, evidence absent | Prompt bytes remain diagnostic; no child authorized by this file alone | A permitted prelaunch replay may verify identical bytes and finish evidence; conflicting bytes fail without overwrite |
   | Prompt+evidence complete | Verify hash/identity, send that exact text as stdin | Files survive launch failure/timeout/abort; never delete them as cleanup |
   | Child may have started or result is uncertain | Existing process/attempt ownership and scheduler reconciliation remain authoritative | Do not infer permission to relaunch from evidence; preserve reservation/identity until existing reconciliation resolves it |
   | Outcome resolved | Existing ingestion decides success/retry/wait/abort | Ordinary next attempt gets a new ID and artifact; successful flow still advances |

   Reader operations never complete a partial publication. Writer idempotence
   is not authorization to rerun an already-launched attempt.
4. Add CLI commands:

   ```text
   integration run reviews RUN_ID [--offset N --limit N]
   integration run review RUN_ID --attempt ATTEMPT_ID
   integration run review-content RUN_ID --attempt ATTEMPT_ID --kind prompt|response|review-markdown|cursor-fix-prompt [--offset N --limit N]
   ```

   Index is paginated by iteration then phaseAttempt then attemptId; all failed,
   active and successful Codex review attempts are included. Attempt ownership
   is checked against RUN_ID. Wrong run or nonexistent attempt is NOT_FOUND.
   Non-review attempt kind is INVALID_ARGUMENT. Iteration alone is not a key.
5. Index includes safe metadata: attemptId/iteration/phaseAttempt/status,
   reviewMode (bootstrap/resume), frozen model/reasoningEffort, timestamps,
   nullable findingsCount/highestSeverity/testsStatus, and content availability.
   Detail includes the same metadata, `response` mapped field-by-field from the
   validated existing result into camelCase (including reviewMarkdown and
   cursorFixPrompt), and prompt/output availability descriptors. Full result
   is bounded by existing MAX_CODEX_REVIEW_RESULT_BYTES (1 MiB); do not inline
   the larger prompt. No free-form result summary/Markdown in the index.
   No raw scheduler metadata/argv. Safe continuity key `reviewerSessionRef` is
   the lowercase SHA-256 hex of UTF-8 `ai_dev_loop:reviewer-session:v1:`
   concatenated with the exact bound reviewer UUID;
   null if unavailable. Derive from that attempt's trusted binding/outcome,
   never a different attempt or today's controller session.
6. `review-content` uses byte chunks for all four kinds. Response returns the
   stored per-attempt response bytes after verifying the authenticated outcome
   binding/hash, even when the captured response is invalid JSON or fails the
   review schema. Exposing diagnostic bytes does not accept a review decision.
   Detail includes resultState (`valid`, `invalid`, `not_produced`, `pending`);
   invalid results have response:null and null findingsCount, never zero.
   Markdown and correction prompt come only from that attempt's valid result
   fields, UTF-8 encoded exactly; do not read the iteration-wide fix-prompt
   path that later attempts can overwrite. A null fix prompt is `not_applicable`;
   invalid results make those derived content kinds unavailable with
   `invalid_result`, while the original response bytes remain inspectable.
   A declared result/hash mismatch is DATA_INTEGRITY. An in-progress/unbound
   result is `not_yet_produced`; a resolved failed attempt with no captured
   result is `not_produced`. Neither is accepted review evidence.
7. Evidence readers distinguish: old completed attempt with no prompt evidence
   = `not_recorded`; active attempt before publication = `not_yet_produced`;
   partial prompt without evidence = `incomplete_capture`; declared evidence
   with missing/mismatched prompt or identity = DATA_INTEGRITY. No reconstruction
   using current code/source files. Historical results already stored remain
   inspectable through their authenticated per-attempt association; ambiguous
   iteration-only leftovers are unavailable, never assigned by guessing.
8. Set API 1.3 and `reviewInspection:true`, preserve previous capabilities and
   fixtures. Output descriptors do not imply processOutput=true until 21.5.

| ID | Mandatory production contract | Observable acceptance |
|---|---|---|
| C-01 | Scheduler runner final prompt → durable evidence → launch | Stored bytes/hash equal actual fake-child stdin, including retry prefix |
| C-02 | Writer failures/interruption → existing attempt reconciliation | No pre-evidence launch, no overwrite/relaunch authority, eventual ordinary completion |
| C-03 | Reviews/review CLI → actual attempt/result association | All attempts distinguished; metadata, findings, response and identity continuity correct |
| C-04 | Review-content CLI → exact per-attempt evidence | Prompt/response/Markdown/fix prompt correct after another attempt writes iteration artifacts |
| C-05 | Historical and partial evidence handling | Explicit absence/invalidity without fabricated prompts or zero findings |
| C-06 | Public/private schema, privacy and lifecycle regression | New evidence canonical, old consumers readable, no content leak or identity/policy change |

## Testing Criteria

Add `tests/unit/integration_api/test_review_projection.py`,
`tests/unit/scheduler/test_phase21_4_review_prompt_evidence.py`, and
`tests/integration/test_phase21_4_review_inspection.py`.

| Contract | Level and acceptance scenario |
|---|---|
| C-01 | Run the actual scheduler Codex attempt entry point with a fake codex executable that reads stdin and checks prompt/evidence already exist; independently hash exact stdin; bootstrap/resume/operational retry |
| C-02 | Deterministic faults before prompt, between prompt/evidence, after evidence and at launch; same-byte prelaunch publication replay and conflicting replay; actual ingestion progresses on successful attempt and preserves existing uncertain ownership |
| C-03 | Two attempts in one iteration and a later review iteration; literal IDs/findings/model/timestamps; wrong-run attempt denied; exact reviewer reference preserved across same-reviewer continuation |
| C-04 | Different fix prompts from two attempts; overwrite iteration-level convenience artifact, inspect each and get its original result text; multi-chunk prompt and response |
| C-05 | Fixture created by pre-21.4 writer format, missing/partial/new corrupt evidence, failed invalid JSON remains inspectable as raw response bytes with structured response null, no fix needed; never reconstruct or falsely report accepted review |
| C-06 | Complete valid/invalid evidence payloads through model+JSON schema, atomic/private files; sentinel content excluded from index/status/history/errors; existing review outcome and recovery regression suites |

The fake child, not test setup, must witness production-created prompt evidence.
A test which prewrites the new evidence before calling the runner cannot prove
C-01. For interruption tests, use explicit fault hooks/events rather than sleeps.
Preserve current timeout, bounded capture, exact identity, abort and usage-wait
behavior. Do not invoke real Codex or create a new scheduler transition.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/integration_api tests/unit/scheduler/test_phase21_4_review_prompt_evidence.py tests/integration/test_phase21_4_review_inspection.py tests/unit/scheduler/test_phase17_5_codex_corrections.py tests/integration/test_phase20_8_sequence_review_retry.py
```

## Validation

Run the phase-specific tests above, then the repository checks once the final
implementation is ready. Use the documented `uv` workflow, not Docker or system
Python changes. Report each exact command and exit/result as executed, failed,
or unexecuted. Do not claim a full rerun from a focused correction rerun.

```bash
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m build
uv run mkdocs build --strict
```

Completion report: for every contract ID, list production entry point/callers,
implementation files, acceptance test names, and actual validation results.
Mandatory missing behavior or coverage is incomplete scope, not residual risk.
On corrections, retain reviewer finding IDs and report fixed/disputed/blocked
with evidence; explain the invariant missed by an earlier incomplete fix.
Do not impose unrelated hardening or exhaustive unsupported Git cases as new
acceptance requirements. Do not stage generated build/site output.

## Risks Or Recovery Notes

This phase has the first new durable writer in Phase 21. A query remains
read-only; capture belongs exclusively to an already-authorized review attempt.
Failures must pass through existing safe runner failure/ingestion paths, not a
new scheduler state. Older prompts were never saved and cannot be recovered
exactly. No backfill or migration of old attempts is authorized. Do not alter
prompt wording or Codex result schema just to make inspection easier.

## OpenQuestions

None.
