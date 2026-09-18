# Phase 21.5 — Live Process Output Inspection

Status: implementation plan for approval; no execution authorized by this file.
Target local Integration API: `1.4`.

## Goals

Expose captured Cursor/Codex stdout and stderr by exact process attempt, during
execution and after completion, using bounded byte reads with truthful source
truncation and completion metadata. Preserve existing process control behavior.

## Non-Goals

Do not implement WebSocket streaming, background tail daemons, arbitrary file
reads, unlimited response payloads, output retention/cleanup, or new capture
limits that change scheduler decisions. Do not label runner envelopes as model
process output or promise byte recovery beyond what capture retained.

## Scope

Output resolution/projection and CLI; narrowly scoped incremental file capture
and flush support in `process.py`, `runners/cursor.py`, scheduler Cursor/Codex
attempt runners; output observation metadata; tests and API/CLI/observability
documentation. Preserve default behavior for unrelated process callers.

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

Prerequisites: accepted Phase 21.1–21.4 command/schema/read helpers and attempt
association. Recheck implementations before dependent edits; they do not yet
exist at the planning baseline.

- `scheduler/application/attempt_paths.py` and `attempt_envelope.py`: outer
  `attempts/<id>/stdout.txt` commonly contains runner outcome JSON, not Codex
  or Cursor child stdout.
- `scheduler/domain/codex_contract.py`: child events/stderr per iteration/attempt;
  Codex capture already has 8 MiB stdout and 256 KiB stderr limits and metadata.
- `scheduler/domain/cursor_contract.py` and `cursor_attempt_runner.py`: child
  turn events/stderr and metadata paths; `runners/cursor.py::execute_prompt`.
- `process.py::run_process_streaming`: bounded branch incrementally writes but
  `_BoundedTextCapture._emit` does not flush; unlimited branch uses communicate
  and writes artifacts in finally. Current Cursor call uses this latter branch.
  Therefore adding only a reader cannot deliver live Cursor output.
- Existing process tests and `tests/unit/scheduler/test_phase17_5_codex_corrections.py`
  cover stdin, timeout, truncation/drain and registration behavior.

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

1. Add production command:

   ```text
   integration run output RUN_ID --attempt ATTEMPT_ID --stream stdout|stderr [--offset N --limit N]
   ```

   Validate run/attempt ownership and known effect kind through scheduler rows
   and existing evidence before resolving a path. Reuse pure chunk reader.
   Map Cursor run-turn to cursor_attempt_events_rel/stderr_rel and Codex
   bootstrap/resume to codex_attempt_events_rel/stderr_rel. For create-chat or
   other attempts, expose a raw child artifact only when existing evidence
   unambiguously associates it with that attempt; otherwise report
   `not_captured`. Do not use shared artifacts by guess or return envelopes.
   No raw output promise for logical/in-process effects with no child stream.
2. Return the shared chunk DTO plus runId, attemptId, component, stream,
   format (`jsonl` or `text`), processState (actual attempt state), complete,
   and `truncatedAtSource` (true/false/null). `availableBytes` is the observed
   on-disk byte size before this read, not total bytes emitted by the child.
   Source captured-byte counters may be returned separately when actually
   recorded; do not equate decoder byte accounting with persisted file size.
   Cap the read to min(requested limit, observed size-offset); output JSON size
   remains bounded by the chunk limit plus base64/metadata overhead.
3. At an active stream's observed EOF, hasMore=false means no more bytes *now*,
   complete=false and nextOffset=byteOffset+returnedBytes allow polling.
   Once the actual attempt is resolved (completed/failed/cancelled) and writer
   stopped, complete=true; nextOffset is null at EOF. Uncertain/launching/active
   attempts remain incomplete even if no bytes arrive. Source truncation true
   may coexist with an active child; it never means process completion.
   While metadata is not finalized or historically missing, truncation is
   null rather than a made-up false. Final known flags stay visible.
4. Missing stream before launch is `not_yet_produced`, a supported old attempt
   without stored stream is `not_captured`, and an explicit finalized binding
   whose mandatory stream disappeared is DATA_INTEGRITY. Empty existing files
   are available. Reject offsets beyond observed size (except zero for an
   empty stream), invalid streams, cross-run IDs and symlink escapes. Treat
   a detected shrink/replacement during reading as DATA_INTEGRITY; do not
   silently restart a cursor at zero or read a different attempt.
5. Provide narrowly opt-in incremental capture for scheduler model attempts.
   Extend run_process_streaming with a default-off option; when enabled, use
   the existing nonblocking pump even if byte limits are None. Flush capture
   file writes after each nonempty emitted chunk (not fsync per chunk).
   Thread the option through execute_prompt for scheduler Cursor turns and
   enable it for scheduler Codex reviews. Preserve unrelated callers' defaults,
   existing capture byte limits and drain policies, decoder behavior, output
   parsing, argv, stdin delivery, timeout budget and process registration.
   This is disk visibility, not a new output-event broker or capture format.
6. Preserve interruption/abort contract: admitted attempt owns child; existing
   OS identity/registration controls termination; incremental captured bytes
   remain after timeout/abort; no duplicated final append. A write/flush error
   after Popen terminates/reaps the child through existing cleanup before
   propagating a safe error. Never release ownership based on EOF alone.
   Resolved execution still reaches normal ingestion; unresolved attempt
   ownership remains with existing scheduler reconciliation, not the reader.
7. Persist final stream facts for new Cursor turns in a dedicated optional
   versioned `output-observation.json` under that per-attempt iteration directory
   (0600). Schema `scheduler-output-observation-v1.json` (under src/ai_dev_loop/schemas/):
   schema_version=1, run_id, attempt_id, iteration, and stdout/
   stderr objects each with stored_bytes (integer>=0), truncated (boolean).
   Fields required/non-null, no bool/string integer coercion. Atomic writer,
   typed model/schema and reader shipped together. Existing Cursor metadata
   and outcome format need not be rewritten. Missing sidecar in old attempts
   means unknown flags; conflicting identity/size in finalized evidence fails
   safely. Codex uses its existing per-attempt capture metadata and known
   completed outcome; do not invent a second authority for Codex decisions.
8. Set API 1.4 and processOutput=true; earlier v1 fixtures stay frozen. Review
   output descriptors link the exact same attempt/stream operation. Describe
   polling/EOF/unknown-truncation semantics and the historical live limitation.

| ID | Mandatory production contract | Observable acceptance |
|---|---|---|
| C-01 | Output CLI → attempt/effect → actual child capture | Requested attempt's events/stderr, never outer outcome envelope |
| C-02 | Shared bounded reader + output EOF projection | Lossless reconstruction of stored bytes, bounded reads, honest live/complete flags |
| C-03 | Scheduler Cursor/Codex production calls → incremental capture | Small stdout and stderr messages visible before fake child exits |
| C-04 | Process timeout/abort/failure cleanup | No duplicate bytes, orphaned child, changed stdin/limit/ownership semantics |
| C-05 | Output observation schema and historical paths | True/false/unknown flags, identity verified, old data unchanged |
| C-06 | Index/default CLI privacy and public compatibility | Content only on explicit reads; old API fixtures still accepted |

## Testing Criteria

Add `tests/unit/integration_api/test_output_projection.py`,
`tests/integration/test_phase21_5_process_output.py`, and focused regressions
in `tests/unit/test_process.py` and the existing abort test files. Preserve
their current test ownership.

| Contract | Level and independent acceptance |
|---|---|
| C-01 | Actual Cursor/Codex runner fixtures create distinct sentinel child output and outer envelopes; CLI must return child sentinel only; two attempts in same iteration and wrong-run ID |
| C-02 | Join base64 chunks and compare independently supplied UTF-8 bytes including split multibyte boundaries; zero-byte file, EOF, offset errors, source truncation and growing-file reads |
| C-03 | Production scheduler runner calls real process helper with fake child; child emits/flushed messages smaller than buffering threshold then waits on a pipe/event; parent reads via CLI before releasing child |
| C-04 | Existing plus opt-in cases: large stdin child reads/doesn't read, timeout kills descendants, abort, partial output preserved once, post-Popen registration/flush error cleans up |
| C-05 | New final Cursor sidecar and old fixture without it, active unknown flags, finalized Codex truncation true, invalid sidecar identity/schema; no reader-created metadata |
| C-06 | Inspect/reviews/history/error sentinel leak checks; unchanged old API fixtures; no output capture policy changes for callers not opting in |

Use deterministic pipes/events and bounded test deadlines, not sleeping and
hoping output arrived. A test that appends the expected live output file itself
does not prove C-03. At least one CLI request must run while the fake production
child is still alive and after that same child has terminated. No live systemd.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/integration_api tests/integration/test_phase21_5_process_output.py tests/unit/test_process.py tests/unit/test_abort_control.py tests/integration/test_phase17_6_abort_lifecycle.py tests/unit/scheduler/test_phase17_5_codex_corrections.py
```

Record the process/abort regression results separately from the full-suite
result; neither implies that the other was executed.

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

Incremental capture changes a real subprocess boundary and deserves targeted
regression evidence. It must not turn an inspection request into a process
controller. No memory/capture-limit redesign is authorized; Cursor's existing
in-memory parsing remains. Streams are the captured text bytes on disk, not a
guarantee of byte-identical raw OS output before the existing UTF-8 decoder.
Readers do not hold scheduler write locks while a child streams.

## OpenQuestions

None.
