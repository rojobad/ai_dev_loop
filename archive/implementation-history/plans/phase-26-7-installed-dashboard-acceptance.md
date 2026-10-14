# Phase 26.7: Installed dashboard acceptance and future Hub handoff

Status: operator-requested implementation scope; implementation pending.
Sequence position: 7/7. Cursor: grok-4.7-high.
Reviewer: gpt-6.1-sol / xhigh, selected by the operator on 2026-10-14.
One ordinary automatic run, with accepted predecessor dependency and one final
operator walkthrough in 26.7. Planning does not prepare/submit/start the sequence.

## Goals

Prove the complete installed, offline-assets local dashboard, close the single integration gate and provide one manual walkthrough plus an exact future Hub API handoff.

## Non-Goals

Installing over the operator runtime, running model turns, launching this sequence, changing Hub/Bridge code, public deployment or declaring human acceptance automatically.

## Scope

Wheel/sdist/frontend packaging, independently owned installed server/browser acceptance, full repository integration gate once, factual docs, measured findings and manual/handoff artifacts.

## Out of Scope

Root YAML/skills/reviewer contracts, Phase 24/25 implementation, real timers/integrations/ledger, changing live driver, external Git actions and new features.

## Required Context

Planning baseline is `main` at `90262eed1af7c76c46139855e249de4ea8ec0d5e`, observed 2026-10-14:
Phase 23 production plus planning-only Phases 24/25 and the non-interactive prompt
fix. Neither 24 nor 25 is a dependency. At this planning baseline, SQLite schema
is 13 and local API is 1.5, before 26.1 adds dashboard capability/additive minor.
Python >=3.11 and `uv` are the current runtime/tooling.
Recheck relevant accepted code once at admission. A future predecessor plan or
staged patch is not evidence of accepted runtime. If a producer changed, stop
only dependent work and report its exact incompatible/missing contract.

Read [Phase 26 overview](phase-26-local-dashboard.md),
[shared dashboard contract](phase-26-dashboard-contract.md),
[source inventory](phase-26-source-observations.json),
[visual reference](phase-26-reference/README.md), `AGENTS.md`,
`docs/referencia/integration-api.md`, `docs/referencia/cli.md`,
`docs/operacion/estado-artefactos.md` and `docs/operacion/seguridad-privacidad.md`.
The referenced new paths below are implementation outputs, not current prerequisites.

Actual accepted production code and focused evidence from 26.1–26.6. Read their
findings once for unresolved mandatory items. Verify assets built from accepted
sources, Python >=3.11 compatibility, public API/schema registration, localhost
server and browser tooling. This run closes packaging gaps but cannot invent
missing mandatory predecessor behavior as a residual risk. Hub Phase 15 closure
is a sequence launch condition recorded in overview, not a dependency on Django
runtime inside this local dashboard implementation.

## Cursor Rules And Skills

Follow `AGENTS.md` and these repo-local rules:

- `.cursor/rules/ai-dev-loop-governance.mdc`
- `.cursor/rules/ai-dev-loop-orchestrator-contracts.mdc`
- `.cursor/rules/ai-dev-loop-state-and-schema-contracts.mdc`
- `.cursor/rules/ai-dev-loop-loop-and-resume-contracts.mdc`
- `.cursor/rules/ai-dev-loop-abort-contracts.mdc`
- `.cursor/rules/ai-dev-loop-codex-review-contracts.mdc`
- `.cursor/rules/ai-dev-loop-docs-acceptance-contracts.mdc`
- `.cursor/rules/ai-dev-loop-global-integrations-contracts.mdc`
- `.cursor/rules/ai-dev-loop-dashboard-contracts.mdc`

Read `.agents/skills/create-cursor-plan/SKILL.md`; for documentation/evidence,
read `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`, applying its
current source/privacy requirements rather than historical Phase 8 restrictions.
The separate final reviewer follows
`.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`; implementation does
not invoke or modify it. There is no `.cursor/skills/` directory. Codex authored
the new dashboard rule/shared contract; Cursor does not change those policies,
planning/review skills or reviewer/correction/result schemas to pass acceptance.

## Architecture Guardrails

One local installation owns the data. SQLite reads use the read-only store and
validated snapshots/lineage; the new SQL read adapter belongs inside ai_dev_loop.
No scheduler transition, migration, reservation, repair, Git, model turn, timer
or real workstation installation is a dashboard side effect. Initial execution
means the first owned execution launch request, excluding chat creation/probes;
project and lineage come from authoritative fields, not names. Keep public DTOs
separate from internal models and ECharts options. Enforce the shared contract's
query/page/envelope/time bounds; missing evidence and partial pages are explicit.

The local server has no login and binds only 127.0.0.1 with typed same-origin read
routes. Sensitive content uses existing confined, verified, paginated readers,
loads only through deliberate UI actions, remains memory-only and is not logged.
UI components use DashboardDataSource; transports/SQLite never own chart logic.
Requests and content retire on scope/selection changes; old responses cannot
restore data. Preserve existing major compatibility and unsupported historical
semantics. Capacity is current, independently dated, and never time-filtered.

Keep root ai_dev_loop.yaml, Phase 24/25 plans, scheduler execution/recovery,
installed providers/drivers, user integrations and live source ledger/artifacts
unchanged. A stable installed scheduler driver must run this self-hosted sequence;
using an editable changing checkout as the live implementation driver is outside
scope. Explicit sequence start authorizes only existing non-final checkpoint
commits; agents do not create their own commits, push or final acceptance.

The shared contract fixes transport parameters/DTOs, metric ownership, lifecycle
and explicit safety bounds; complete this plan's owned slice without broadening
adjacent work. Read the exact public/shared types rather than reproducing rules
from display strings. Any incompatible prerequisite is a dependent-work blocker.

## Implementation Plan

1. Complete explicit frontend-to-wheel packaging: build dashboard-web, include
   web_dist/static manifest and new schemas in wheel; include frontend/lock/build
   sources in sdist. Ship no node_modules, real ledger/artifacts, test captures,
   credentials, source maps containing private content or external runtime CDN.
   Missing build assets must fail the package build instead of shipping placeholder
   dashboard. Ordinary installed wheel serve needs neither Node/npm nor checkout.
2. Create `scripts/verify-phase26-dashboard.py` with --list/--case and accept/
   prepare/cleanup modes. Own a temporary wheel-installed venv, isolated XDG,
   known fixture ledger/artifacts, loopback port and processes in one recorded
   private resource directory; cleanup only those resources with identity checks.
   No user installation/timer action; uncertain cleanup preserves recovery info.
3. Exercise a wheel-installed CLI and real HTTP/browser path without source
   PYTHONPATH/import fallback: project+range -> overview -> replacement map run ->
   timeline attempt -> modal -> explicit prompt/stdout/stderr; activity all modes;
   Pause/hidden/manual Refresh; unavailable/historical/active output; offline bundled
   assets. Capacity uses a fake provider. Verify exact independent expected metrics,
   request/ownership/coverage and no dashboard writes to source scheduler state.
4. Execute the one repository-wide pytest integration gate with fake providers
   and isolated temp env, plus existing static/package/docs gates and frontend
   suite/browser journey. Measure nonzero assertions/cases/exit/count/elapsed time;
   do not nest previous full acceptance scripts or rerun the full gate in every
   correction. Changed failure selectors own correction reruns; report their scope.
5. Write current Spanish docs `docs/operacion/dashboard-local.md` and
   `docs/referencia/dashboard.md`, update implemented CLI/API/installation/state-
   privacy pages and MkDocs nav. Include command, port, read scope, data/coverage/
   metric definitions, local-only access, source build, wheel launch, Pause/Refresh
   and honest exact-prompt limits. Codex planning policies stay unchanged.
6. Write `archive/implementation-history/findings/phase-26-7-installed-dashboard-acceptance.md`,
   `archive/implementation-history/plans/phase-26-manual-walkthrough.md` and `archive/implementation-history/plans/phase-26-to-hub-dashboard-handoff.md`.
   Manual checklist names the prepared fixture site/resources and optionally a
   source-account read-only observation; no model execution turn is required.
   prepare leaves owned usable fixture site, cleanup is separate. Native scheduler
   state is recorded only if observed; green tests/commit are not human acceptance.
   Final implementation stays staged awaiting separately authorized finalization.
7. The handoff records actual API version/capability/DTO schemas, normalized
   filter/metric semantics, request/result/time bounds, asset/component/data-source
   boundaries and exact additional Bridge/hub/Live/cache work. It must not label
   future Hub operations implemented or authorized.

## Testing Criteria

Mandatory contracts are the acceptance boundary. Add/update automated unit,
contract/integration and browser tests as listed; test the real producer/consumer
with independent expected outputs, not a duplicate implementation or a test-only
safety guard. IDs belong to this run and must appear in its completion report.

| ID | Real producer -> consumer | Input/precondition | Independent expected result / defect caught |
| --- | --- | --- | --- |
| C267-1 | Built wheel -> isolated installation -> real server/browser | New installed CLI outside source checkout, no Node on runtime PATH, external network blocked, fixture ledger/content and >one map/timeline page. | All seven widgets/drill-down/actions work with known expected data; wheel contains correct schema/assets; no source import fallback or CDN. Catches editable-checkout/dev-server-only acceptance. |
| C267-2 | Owned harness modes -> processes/files -> cleanup or prepared site | --list, scoped cases, accept, prepare, foreign/uncertain cleanup and actual recorded fixture resource cleanup. | Nonempty inventory, bounded readiness/teardown, refusal preserves foreign resources, prepare usable with manual-pending status. Catches cleanup deleting user state or automatic human acceptance. |
| C267-3 | Public CLI/HTTP/components -> acceptance evidence/docs/handoff | Real fixture journey, legacy evidence missing, unchanged scheduler supported tests, installed schema/capability and documentation commands. | Independent semantic assertions and successful once-owned integration/package/docs gates; actual version/bounds match handoff; human acceptance explicitly pending. Catches tests replacing producer behavior or future claims presented as current. |

## Validation

The owning run introduces and verifies its harness; example future commands:

```bash
uv run python -m build
uv run python scripts/verify-phase26-dashboard.py --list
uv run python scripts/verify-phase26-dashboard.py accept
uv run python scripts/verify-phase26-dashboard.py prepare
uv run python scripts/verify-phase26-dashboard.py cleanup
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q
uv run python -m ruff format --check .
uv run python -m ruff check .
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

The harness documents its recorded directory/port/owner and honors case selectors;
these commands are design, not already verified. Package checks inspect/install
built artifacts in an isolated venv; installed process cwd must be outside repo
and imports resolve to the wheel. Test Python 3.11 and the supported development
Python without changing system Python; an unavailable required interpreter is an
explicit blocker. In dashboard-web run existing typecheck/unit/build/browser
commands. Coordinate browser acceptance through one owned installed setup, target
<=40 minutes scoped setup/journey/cleanup; record the once-only repository suite
separately if its measured duration exceeds that budget. Do not relabel unrun
mandatory gates as residual risk or claim live provider/workstation deployment.

These are planned implementation checks, not results of plan authoring. Use
isolated temporary XDG/homes/repos and fake providers; tests must not launch real
Cursor/Codex model turns, tick/control the live scheduler or change installed
services. Report exact commands, exit codes, elapsed time, executed/failed/unrun
checks and every contract ID -> production caller -> independent assertion ->
result in the owning findings file. Mandatory missing behavior/tests remain
incomplete scope. After correction rerun failed/affected checks; expand only for
a concrete remaining risk. Full repository pytest belongs once to 26.7's
integration gate, not to every run/review. Target <=40 minutes focused validation
per subphase, keeping setup/cleanup owned and bounded. A failed required check
blocks dependent work; informational READY_FOR_REVIEW/BLOCKED_CONTRACT labels
are not new scheduler states.

## Risks Or Recovery Notes

No real workstation install is authorized by this implementation plan. prepare
must leave only its recorded fixture server; operator read-only source observation
is optional and clearly distinct. Integration success is separate from final Git
commit, Hub Phase 15 acceptance and human Dashboard acceptance. Preserve foreign
resources/cleanup markers on uncertainty and provide concrete next action.

## OpenQuestions

None.

Product scope and reviewer choices are defined at authoring. Missing actual accepted predecessor,
incompatible contract or unavailable mandatory validation environment found at
admission is a new concrete execution blocker: report the evidence and required
decision; stop dependent work without inventing a private fallback.
