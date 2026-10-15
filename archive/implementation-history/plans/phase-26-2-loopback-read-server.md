# Phase 26.2: Loopback HTTP read server

Status: operator-requested implementation scope; implementation pending.
Sequence position: 2/7. Cursor: grok-4.7-high.
Reviewer: gpt-6.1-sol / xhigh, selected by the operator on 2026-10-14.
One ordinary automatic run, with accepted predecessor dependency and one final
operator walkthrough in 26.7. Planning does not prepare/submit/start the sequence.

## Goals

Serve a bounded local read API from the operator OS account, calling the accepted query/content services in process, with a working new dashboard serve command.

## Non-Goals

Login, public/LAN exposure, remote shell/control, background daemon, chart UI or integration installation.

## Scope

Bounded standard-library HTTP adapter, typed allowlisted operation dispatch, fixed loopback CLI, provisional package page, response policy and lifetime/error tests.

## Out of Scope

Node/frontend, browser polling, scheduler mutation/writer contracts, root YAML, real timers/drivers and live workstation setup.

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

Actual accepted 26.1 production query/CLI/schema/capability implementation;
verify its four entry points and scoped tests. Existing Integration API services
for frozen prompts, review detail/content, stdout/stderr and Codex capacity are
implemented on planning baseline, as are read-only default XDG resolvers. The
artifact authentication API is about evidence ownership, not user login. Do not
replace those readers with a raw artifact directory HTTP server.

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

1. Add `dashboard/adapters/http.py`, `dashboard/server.py` and
   `commands/dashboard.py`; register new `dashboard serve --port` without
   touching scheduler command/state behavior. Bind exactly 127.0.0.1 and validate
   port; collision/invalid input exits safely with no storage creation. This
   slice serves an honest English provisional page from a package-owned asset.
2. Implement the shared POST read dispatcher using explicit per-operation params
   and DTO projections. Summary operations call 26.1 service directly; exact run,
   sequence, frozen/review/output/capacity operations call existing services.
   Permit zero arbitrary path/command/SQL access. Invalid input fails before work.
3. Enforce exact Host/Origin, request bytes/methods, no-store and response bounds,
   safe errors and static manifest containment. Emit no bodies, query params,
   private artifact paths or upstream stderr in access/error logs. Do not add a
   general directory listing or CORS enablement option.
4. Bound handler admission to four and service work to two without an unbounded
   thread/task queue. Bound socket reads/writes and total work, release permits
   in finally, use per-request SQLite connections; canceled/disconnected readers
   cannot become write work. Capacity is one bounded explicit probe and cannot
   starve cleanup indefinitely. The HTTP adapter opens no enclosing DB transaction across service/file/probe/
   send; preserve existing bounded verified content-reader lifetimes.
5. Test actual listen/accept/read/close on an owned temporary loopback port and
   isolated XDG, plus deterministic saturation/timeouts and SIGINT retirement.
   Stop owns only its server/jobs; restart creates no scheduler recovery/repair.
   Add factual CLI/operational docs and record
   `archive/implementation-history/findings/phase-26-2-loopback-read-server.md`.

## Testing Criteria

Mandatory contracts are the acceptance boundary. Add/update automated unit,
contract/integration and browser tests as listed; test the real producer/consumer
with independent expected outputs, not a duplicate implementation or a test-only
safety guard. IDs belong to this run and must appear in its completion report.

| ID | Real producer -> consumer | Input/precondition | Independent expected result / defect caught |
| --- | --- | --- | --- |
| C262-1 | CLI -> bound server -> real HTTP client | Valid temporary port, invalid/out-of-range port, occupied port and Ctrl-C/restart with isolated XDG. | Only loopback listens; CLI never accepts remote bind; clean exit releases port, leaves ledger/artifacts byte-for-byte/content metadata unchanged and creates no scheduler work. |
| C262-2 | Typed HTTP dispatch -> accepted services -> envelope | Same semantic requests through CLI and HTTP, exact owned attempt content with correct and wrong run/attempt IDs; invalid method/Host/Origin/body/op. | HTTP/CLI semantic results match modulo observation times; rejected requests dispatch nothing; wrong ownership fails via existing reader. Catches generic path dispatch and duplicate query logic. |
| C262-3 | Handler admission/deadlines -> read/probe worker -> shutdown | Hold real production readers with synchronization; exceed two service/four handler admission, expire socket/query budget and disconnect. | Bounded counters/threads/child lifetime, safe saturation and released permit; later normal read succeeds. Catches ThreadingMixIn spawning unbounded threads or stuck slot after timeout. |
| C262-4 | Static manifest and response adapter -> browser-compatible client | Provisional index, owned asset, traversal/encoded traversal/unlisted file, internal service error and sentinel content. | Only manifest assets served; expected headers/no-store; no sentinel/private paths in logs/error. Catches serving artifact roots or exposing tracebacks. |

## Validation

Create `tests/unit/dashboard/test_http_dispatch.py`, `test_server_policy.py`
and `tests/integration/test_phase26_2_dashboard_server.py`. Run:

```bash
uv run python -m pytest -q tests/unit/dashboard tests/integration/test_phase26_2_dashboard_server.py tests/unit/integration_api/test_process_output_auth.py tests/unit/integration_api/test_review_evidence_auth.py
uv run python -m ruff check src/ai_dev_loop/dashboard src/ai_dev_loop/commands/dashboard.py tests/unit/dashboard tests/integration/test_phase26_2_dashboard_server.py
uv run python -m mypy src
uv run mkdocs build --strict
git diff --check
```

The integration test launches the real server CLI with a copied subprocess env
and temporary XDG (do not overwrite HOME/CODEX_HOME in the operator shell), makes
real HTTP requests, waits for readiness with a deadline and reaps its process.
Fake capacity executable/services are the only provider inputs. New serve/help
syntax must execute here before docs call it verified.

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

The server is a local adapter with no remote deployment guarantee. Handler
shutdown cannot kill an unrelated scheduler/model process. Runtime errors retire
only owned read work. SQLite/artifact reads preserve their original authority;
provisional frontend status must not pretend charts are implemented.

## OpenQuestions

None.

Product scope and reviewer choices are defined at authoring. Missing actual accepted predecessor,
incompatible contract or unavailable mandatory validation environment found at
admission is a new concrete execution blocker: report the evidence and required
decision; stop dependent work without inventing a private fallback.
