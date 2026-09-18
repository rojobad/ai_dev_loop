# Phase 21.6 — Codex Capacity and Complete Integration Acceptance

Status: implementation plan for approval; no execution authorized by this file.
Target local Integration API: `1.5`.

## Goals

Expose an on-demand local capacity observation with available validated quota
percentages/windows, while preserving scheduler decisions. Prove the entire
supervision contract through a fake Bridge consumer of the public CLI only.

## Non-Goals

Do not add a quota history database, automatic polling service, account switcher,
remote control operations, scheduler resume authority, or real provider/model
validation. Final acceptance is the local integration deliverable, not a claim
that the separate Web UI/Bridge has been implemented or deployed.

## Scope

Extend the existing bounded capacity probe with optional diagnostic projection;
add public capacity DTO/command and schemas/fixtures; whole-API compatibility
and fake-client acceptance tests; finalize Spanish API/CLI/observability/privacy
docs and `archive/implementation-history/findings/phase-21-integration-acceptance.md`.

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

Prerequisites: independently accepted Phase 21.1–21.5, including actual live
output implementation/tests, and current Phase 19/20.5 capacity behavior.
Verify earlier contracts at admission, not by assuming plans were executed.

- `scheduler/application/codex_capacity_probe.py`: interactive initialize →
  initialized → account/rateLimits/read, validated bounded parser, available/
  exhausted/unavailable classifier; CodexCapacityObservation currently retains
  only status/reason, so percentages require a real producer extension.
- `scheduler/application/codex_workflow_service.py`: scheduler calls the probe
  using the frozen run command; waiting/retry policy must remain unchanged.
- `scheduler/application/codex_subprocess_env.py`, `domain/codex_contract.py`:
  sanitized local environment, 15s timeout, 256 KiB stdout/64 KiB stderr bounds.
- `tests/unit/scheduler/test_phase19_codex_capacity.py`, Phase 20.5 capacity
  tests in `tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py`,
  and existing fake app-server helpers establish supported wire forms and
  null-marker/classification regressions.
- All earlier Phase 21 tests/fixtures and Phase 20.9 multi-run acceptance tests.

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

1. Add `integration codex-capacity [--output json]`. For this single-installation
   contract, resolve the locally installed `codex` command via PATH under the
   Bridge's OS user and the existing sanitized environment. Do not accept a
   remote-selected executable/path/config/session/account parameter. This
   observes that local Codex context; it is not a promise that every run on a
   machine using a different frozen executable shares an account. Document
   this simple deployment requirement; no account discovery/identity output.
2. Reuse CodexAppServerCapacityProbe and its interactive transport/parser in
   process. Extend the typed observation with default-empty diagnostic limits,
   retaining compatibility with current status/reason-only fake probe callers.
   Do not duplicate subprocess protocol, relax validation or persist telemetry.
   `info`/run/sequence inspection never triggers this probe; only this explicit
   command may start one bounded local app-server query. No retries or caching.
3. Public `data`: status, nullable safe reason, and `limits` array; envelope
   observedAt is the time observation completed, not when a run last waited.
   Each limit object has id, window (`primary`/`secondary`), usedPercent,
   remainingPercent, nullable windowDurationMinutes and resetsAt. Derive records
   from supported `rateLimitsByLimitId` map, preserving map key as id; for the
   existing supported singleton `rateLimits` form use id=`default`. Preserve
   current map/singleton precedence from _scan_limit_records; don't merge two
   competing sources. Sort by id then primary/secondary.
4. A usedPercent is a finite JSON number >=0, excluding booleans/strings. Keep
   values above 100 as observed; remainingPercent=max(0,100-usedPercent).
   Missing/null windows do not invent observations. Optional protocol fields
   `windowDurationMins` (positive integral minutes) and `resetsAt` (nonnegative
   integral Unix seconds) become normalized duration and UTC timestamp only
   when valid/representable. Reject bool/string coercion for those fields;
   absent/null/malformed optional display fields become null and do not alter
   capacity classification. Unknown protocol fields are ignored, never echoed.
   New fixtures for these optional wire fields are explicit supported-input
   examples, not claims of captured historical provider output.
5. Status must equal the existing classifier for every previously supported
   payload, including exhausted markers and multiple limit records. A marker
   can mean exhausted with limits:[]; never fabricate 100% usage. A malformed
   core response makes unavailable with limits:[]; preserve existing priority
   of real exhaustion over malformed sibling records. Only valid numeric
   windows may appear in a non-unavailable observation. Provider errors,
   unavailable binary, timeout/cleanup/protocol/output-limit failures return
   a successful observation envelope with status=unavailable and safe reason,
   not 0% used and not raw stderr/account payload. CLI misuse remains an error.
6. No scheduler ledger or protected-artifact writes from this command. The
   child may read/use its normal local Codex runtime configuration as the
   existing probe does; do not claim the external executable cannot touch its
   own runtime files. Queries do not authorize execution or resume a wait;
   scheduler continues its independent probe of the frozen command and exact
   reviewer. Preserve process-group cleanup under the existing bounded probe.
7. Set API 1.5 with all five capabilities true after the entire suite passes.
   Freeze `v1_5` responses and retain v1_0 through v1_4. Add
   `tests/integration/test_phase21_6_integration_acceptance.py` and a lightweight
   fake Bridge client under tests only. It must call public CLI argv, parse
   responses, and resolve all resource/content IDs through responses. It may
   not open SQLite, use private projection imports, or traverse artifact roots.
   Test setup may create fixtures via real local services; the client may not.
8. Acceptance client traverses: info → standalone runs + sequences → phases
   with zero/one/multiple runs → run budget/history/timeline/attempts → frozen
   plan/initial prompt → every review + prompt/response/Markdown/fix prompt →
   stdout/stderr chunks during/after execution → final sequence report →
   capacity. Reconstruct content exactly and compare to independent fixture
   constants. Include residual risk, waits, failed reviews and historical
   missing prompts. No feature is dropped to claim a smaller first release.
9. Compatibility acceptance: pinned older same-major consumers tolerate new
   optional fields/capabilities, new consumer reads older public fixtures with
   their capability gates, and differing-major info results in UPDATE_REQUIRED
   with zero subsequent requests. Fixture baselines must come from each actual
   accepted subphase, not regenerated wholesale by the final serializer.
   Upstream protocol version is distinct from package version and future
   Agent↔Server protocol. No remote protocol implementation here.
10. Publish final docs and a findings/acceptance artifact listing C-IDs from all
    six phases, production/test evidence, exact commands/results, unavailable
    historical data, and any blockers. Explain sensitive on-demand content,
    single-user local boundary, polling/chunk decoding, side effects of the
    explicit capacity probe, and unsupported remote mutations. No manual live
    smoke is required for automated acceptance; label live provider validation
    unexecuted unless separately authorized, never imply it ran.

| ID | Mandatory production contract | Observable acceptance |
|---|---|---|
| C-01 | Capacity CLI → existing interactive local probe | Valid percentages/windows from fake app-server, bounded startup/query/cleanup |
| C-02 | Existing scheduler probe consumers/classifier | Same decisions on old fixtures, no quota-driven state writes or session changes |
| C-03 | Normalization and unavailable observation | Null/unknown/malformed cases honest, no raw auth/provider content |
| C-04 | Fake Bridge → only public CLI for entire inventory | Complete supervision including multi-run phases, content, live output and report |
| C-05 | Pinned cross-minor/major fixtures and consumer | Same major accepted, additive fields ignored, mismatch stops all resource calls |
| C-06 | Whole-product regression, package and documentation | Existing scheduler flow preserved; documented 1.5 matches packaged implementation |

## Testing Criteria

Add `tests/unit/integration_api/test_capacity_projection.py`,
`tests/integration/test_phase21_6_capacity.py`, and
`tests/integration/test_phase21_6_integration_acceptance.py`.

| Contract | Level and independent acceptance |
|---|---|
| C-01 | Real CLI launches fake PATH codex app-server that enforces initialize/initialized/request ordering; success, timeout, EOF, oversized response and cleanup failure |
| C-02 | Existing Phase 19/20.5 golden classifier fixtures and actual waiting scheduler path; capacity CLI changes no run/event/reservation rows; scheduler still passes its frozen executable |
| C-03 | Map/singleton, primary/secondary, finite/int/fractional/>100 percent, bool/string/NaN rejection, missing/null marker, exhausted marker without numeric window, optional duration/reset absent/invalid; literal expected values |
| C-04 | Full real-CLI fake-client traversal with known fixture content, real retry lineage setup and deterministic live fake children; client prevented from private file/DB access |
| C-05 | Saved old API fixtures plus older strict-required-field consumer; injected unknown additive fields/new optional capabilities; request counter stays zero after mismatched-major info |
| C-06 | All Phase 21 tests and full repository suite, wheel in temporary isolated environment, schema loading/CLI info, strict MkDocs build |

Capacity test children must never discover real credentials or contact a real
account; use temporary XDG/HOME and existing fake CLIs. Sanitization tests keep
the established native WSL versus Windows CODEX_HOME policy. No new network
dependency for contract tests or package smoke. Use all earlier phase fixtures
unchanged and genuine historical scheduler fixtures for old read support.

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q tests/unit/integration_api tests/integration/test_phase21_1_integration_contract.py tests/integration/test_phase21_2_run_inspection.py tests/integration/test_phase21_3_sequence_inspection.py tests/integration/test_phase21_4_review_inspection.py tests/integration/test_phase21_5_process_output.py tests/integration/test_phase21_6_capacity.py tests/integration/test_phase21_6_integration_acceptance.py tests/unit/scheduler/test_phase19_codex_capacity.py tests/unit/scheduler/test_phase20_5_reliable_codex_limit_detection.py
```

The command above includes the existing Phase 20.5/null-marker regressions.
The final required full suite covers all scheduler recovery regressions.

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

Capacity is a point-in-time observation of local Codex context and can become
stale immediately; observedAt expresses that without introducing local cache.
The app-server protocol may evolve; unknown optional diagnostics stay null,
malformed core data becomes unavailable, and scheduler decisions remain under
its existing parser. Completeness refers to all local supervision operations;
remote start/abort/retry and a deployed UI remain outside this deliverable.

## OpenQuestions

None.
