# Phase 21.1 — Local Integration Contract Foundation

Status: implementation plan for approval; no execution authorized by this file.
Target local Integration API: `1.0`.

## Goals

Establish the machine-readable local boundary that the future Bridge will use.
Deliver a working `integration info` and the shared response/error contracts,
without falsely advertising resource readers that are not implemented yet.

## Non-Goals

Do not implement run/sequence readers, evidence capture, process output, capacity
probing, or the Bridge. Do not enforce remote protocol policy inside scheduler.

## Scope

New integration CLI registration, public typed DTOs/JSON schemas, compatibility
fixtures and consumer tests, safe error handling, and Spanish product docs.
Create `docs/referencia/integration-api.md` and link it in `mkdocs.yml`; update
`docs/referencia/cli.md` for the implemented command only.

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

- `src/ai_dev_loop/cli.py`: Typer root, existing `integrations` namespace,
  `OutputFormat`, and `_handle` (human errors are not this new JSON contract).
- `src/ai_dev_loop/scheduler/application/status.py` and
  `scheduler/infrastructure/sqlite_store.py`: existing read-only boundaries.
- `tests/unit/scheduler/test_status_readonly.py` and
  `test_schema_readonly_historical.py`: no bootstrap/chmod during inspection.
- `src/ai_dev_loop/schemas/`, package resource loading, and `pyproject.toml`:
  package schemas as resources without new runtime dependencies.
No earlier Phase 21 implementation is a prerequisite for this phase.

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

1. Register `integration` with a small dedicated CLI adapter. All commands in
   this namespace emit JSON by default and accept `--output json` at the leaf.
   `--help` stays ordinary help. Text output is not introduced in this API.
2. Implement the following public wire contract at API `1.0`:

   ```json
   {"apiVersion":{"major":1,"minor":0},"ok":true,
    "observedAt":"2026-09-18T12:00:00Z","data":{},"error":null}
   ```

   Failures use the same envelope, `ok:false`, `data:null`, and
   `error:{"code":"INVALID_ARGUMENT","message":"safe explanation"}`.
   Emit exactly one JSON document on stdout, with no debug banners, raw exception
   text, input echo, or traceback. Integration-scoped argument/unknown-command
   errors also use this envelope (exit 2); do not change root/plural-namespace
   parsing. Successful calls exit 0; stable failures: INVALID_ARGUMENT=2,
   NOT_FOUND=3, UNSUPPORTED=4, DATA_INTEGRITY=5, IO_ERROR=6, INTERNAL_ERROR=1.
   Unexpected exceptions return a generic safe error; do not hide interrupts.
3. `integration info` returns package version as `aiDevLoopVersion`, and boolean
   capabilities `runs`, `sequences`, `reviewInspection`, `processOutput`,
   `codexCapacity`, initially all false. Version is the API contract version,
   independent of package version. The command works without scheduler storage
   or executable discovery and creates nothing. Do not probe Codex in info.
4. Define reusable conventions now, implement resource-specific DTOs in their
   owning phases: camelCase JSON; nonempty opaque resource IDs; UTC RFC3339
   timestamps; counts/ordinals/offsets as JSON integers excluding booleans;
   no string/float coercion in public JSON DTOs. CLI decimal integer strings
   are parsed normally. First ordinal/iteration is 1; byte/list offset is 0.
   Stable nullable fields are emitted as null when unknown/not applicable;
   arrays use [] for known empty collections. New optional minor fields may
   be absent for older producers; consumers accept missing optional fields.
   Public schemas allow unknown additive object fields; private persisted
   evidence schemas may remain closed. State/kind/reason labels are extensible
   strings; a consumer must preserve/display an unknown label without acting
   on it. Existing field meanings/requiredness never change within major 1.
5. Collection commands in later phases use `--offset 0 --limit 100` (hard max
   500; reject invalid/oversized input), deterministic documented ordering,
   and `items,offset,limit,nextOffset,hasMore`. nextOffset is null at the end.
   One response is a read snapshot; separate pages may reflect later writes.
   No cross-request snapshot tokens or cryptographic cursors. Queries apply
   bounds before projecting rows; no all-ledger artifact crawl.
6. Establish a shared explicit artifact-chunk DTO for later phases:
   `available:boolean`, nullable `reason`, `encoding:"base64"`, mediaType, byteOffset, returnedBytes,
   nextOffset, availableBytes, hasMore, contentBase64, and nullable sha256.
   Defaults: offset 0, limit 65536; hard max limit 262144; invalid offsets/limits
   are INVALID_ARGUMENT. Raw-byte transport preserves split UTF-8/binary data;
   clients decode after joining or use incremental text decoding. An available
   zero-length artifact is distinct from absence. Unavailable content has zero
   returnedBytes, empty contentBase64, nextOffset:null, hasMore:false, and a
   stable reason; availableBytes and sha256 are null when unavailable. An
   available artifact has reason:null and an integer availableBytes>=0.
   Do not implement a generic path-taking artifact command.
7. Add independent pinned producer/consumer fixtures at
   `tests/fixtures/integration_api/v1_0/` and unit/CLI contract tests. Fixtures
   are the first public baseline, not purported pre-existing historical API
   data. Later phases retain these fixtures unchanged and add their own.
   A minimal test consumer accepts all same-major payloads/unknown optional
   fields and refuses a different major with UPDATE_REQUIRED before issuing a
   resource request. No universal automated proof of semantic compatibility is
   claimed: schemas, golden fields, and behavior scenarios establish it.

| ID | Mandatory production contract | Observable acceptance |
|---|---|---|
| C-01 | `ai_dev_loop integration info` and real root CLI wiring | Valid 1.0 envelope, package version, honest false capabilities |
| C-02 | Integration argument and runtime error adapter | Stable code/exit, one JSON document, no sensitive diagnostic echo |
| C-03 | Public DTO/schema/consumer conventions | Same major accepted, unknown additions tolerated, different major stops requests |
| C-04 | Info and integration setup | Empty XDG unchanged, no DB/model/Git/systemd access or schema migration |
| C-05 | Packaged schema resources and docs | Installed wheel can load schemas and invoke info; plural integrations unchanged |

## Testing Criteria

Add `tests/unit/integration_api/test_contract.py` and
`tests/integration/test_phase21_1_integration_contract.py`.

| Contract | Level and independent assertions |
|---|---|
| C-01 | CLI integration via the production Typer app and one module/subprocess CLI smoke; literal expected envelope fields/capabilities, not values generated by the same serializer |
| C-02 | Invalid output, missing argument, unknown integration command, injected safe I/O/internal failures; exit codes and parsed stdout, secret sentinel absent in stdout/stderr |
| C-03 | Contract fixtures for valid/invalid nulls, booleans-as-integers, unknown fields, and major mismatch; fake consumer request spy proves zero resource calls after rejection |
| C-04 | Empty temporary XDG tree and filesystem comparison; fail spies on scheduler bootstrap, subprocess/model launch and permission repair |
| C-05 | Existing CLI tests plus temporary wheel installation/schema loading; docs build and help for both singular/plural namespaces |

No live external calls. Use CliRunner for ordinary CLI cases and a hermetic
subprocess for the entry-point smoke. Existing fixtures establish old scheduler
read behavior; do not invent a current schema with only a changed version label.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/integration_api/test_contract.py tests/integration/test_phase21_1_integration_contract.py tests/unit/scheduler/test_status_readonly.py
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

No persisted scheduler shape changes or migrations. Every info/error path must
be usable before installation has created a ledger. Keep parser customization
local to integration; broad Typer error-handler changes risk breaking existing
CLI consumers. Absent DB is not a reason to hide installed capabilities.
Collection/artifact conventions introduced here are public design definitions;
they do not advertise future commands as implemented.

## OpenQuestions

None.
