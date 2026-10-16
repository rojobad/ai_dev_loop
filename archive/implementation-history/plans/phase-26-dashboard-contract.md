# Phase 26 dashboard contract and architecture

Status: implementation design for the operator-requested Phase 26 planning package,
2026-10-14, refreshed 2026-10-15 after accepted Phase 27. None of these new
dashboard commands, schemas or UI paths exists at
planning time. The seven plans implement this contract incrementally. This document
is the shared design, not evidence of runtime acceptance.

## Product decisions and boundary

One local scheduler installation supplies one dashboard, with a project selector
and inspection range. Use the existing authoritative `projectName`, never parse a
run/sequence slug to infer project or phase lineage. All projects on this machine
may be shown. The future Hub dashboard will select one machine and consume the
same public query DTOs. Hub/Bridge implementation is outside Phase 26.

The local dashboard has no login or remote credentials. A Python process bound
only to IPv4 loopback serves packaged React 18 / TypeScript / Apache ECharts 6
assets and a small typed HTTP read adapter. Domain calculations and application
queries are shared with the JSON Integration API. SQLite and registered artifacts
are read by Python inside `ai_dev_loop`; the browser consumes explicit DTOs.
Read connections use `SqliteSchedulerStore.open_readonly` and `begin_read` or a
batched equivalent with the same snapshot/lineage validation. No ledger migration,
bootstrap, repair, scheduler state transition, reservation, Git action, timer,
provider execution turn or durable telemetry store is introduced by dashboard reads.

## Architecture and future reuse

- `src/ai_dev_loop/dashboard/domain/`: framework-independent filters, time and
  metric rules. It does not import HTTP handlers, CLI presentation or SQLite.
- `dashboard/application/`: typed query use cases and ports. It coordinates a
  read repository and existing explicit content/capacity services.
- `dashboard/adapters/sqlite_read.py`: batched scheduler-owned read queries,
  validated state/lineage projections and bounded connection lifetime.
- `integration_api/dashboard_models.py` and `dashboard_service.py`: explicit
  camelCase public DTOs/projections; no database rows/internal model aliases.
- `commands/integration.py`: additive JSON-only `integration dashboard` group.
- `dashboard/adapters/http.py`, `server.py`, `commands/dashboard.py`: thin local
  HTTP/CLI adapters. They call services in process, not subprocess CLI per read.
- `dashboard-web/src/`: presentational components, parsers and a typed
  `DashboardDataSource` interface. Local fetch lives in one adapter; components
  do not know localhost, SQLite paths, Hub URLs, auth or Bridge messages.
- `src/ai_dev_loop/dashboard/web_dist/`: future generated distribution assets,
  produced by the explicit frontend build and included in the wheel.

Expose semantic data, not ECharts options, HTML, colors or scheduler reducers.
Keep exact IDs and raw open state strings; chart grouping is a documented display
projection and cannot determine authority/safe next actions. A later Hub phase
adds typed Bridge handlers, HTTP routes and optional cache/Live allowlists.

## Filters, clocks and collection bounds

`from` and `to` are required UTC RFC3339 instants, normalized as `fromUtc` and
`toUtc`, with `[fromUtc, toUtc)` semantics. Require from < to and span <= 366 days.
`timeZone` is a valid IANA name, defaults to `America/La_Paz`, and is echoed in
results. Python 3.11 `zoneinfo` and browser `Intl` must use that named zone, not
the execution host timezone. Invalid/nonexistent/ambiguous local input is rejected
with a useful message rather than silently shifted; allow an explicit offset to
resolve ambiguity. `projectName` omitted means all projects; explicit null, blank,
non-string or >256 UTF-8 bytes is invalid. A valid unknown project yields a
complete empty result. Identifiers retain existing opaque-ID validation.

Strict integer page offset >=0; limit defaults to 25, max 100. Booleans, strings,
floats and null are not integers. Unknown additive input keys are ignored; they
never become SQL identifiers or options. Params are bound values. Every result
includes the canonical filter echo and has an independently recorded observedAt.
Different calls are separate observations, not one atomic global snapshot.
Within one query, all contributing ledger reads use one bounded read transaction.
New aggregate transactions close before browser send, provider probe or content
inspection. Existing bounded verified content services retain their own read
lifetimes; the HTTP adapter adds no enclosing database transaction.

For summary/runs/activity, admit <=10,000 relevant runs and <=100,000
contributing execution attempts per query, <=5 seconds of ledger query/validation work (monotonic budget; SQLite
progress interruption), and a serialized CLI/HTTP envelope <=240 KiB. These are
initial explicit bounds for the local and future Bridge use case. Project/workload
buckets are also bounded by the envelope, not silently truncated. If the requested
scope exceeds a bound, return existing safe INVALID_ARGUMENT with a fixed
narrow-scope message, without partial totals or raw SQL/paths. Unexpected storage
failures remain INTERNAL_ERROR/IO_ERROR; corruption is DATA_INTEGRITY. A missing
ledger returns complete empty summary/catalog/map/activity without creating it.
Unsupported schema features return UNSUPPORTED. Old supported ledgers missing
optional attempt timestamps produce coverage warnings, never a migration. Catalog
queries are separately paged and time/envelope bounded; they do not materialize
every run/attempt merely to list projects. Catalog limit failures say retry or
reduce page size, rather than advising an unsupported catalog time filter.

Offset pages are fresh observations. Deduplicate visible run IDs and reset pages
on Refresh/filter changes. Return totalCount from the same query snapshot, but do
not claim pages observed at different times are one stable historic snapshot.

## Execution attempts and metric semantics

Use explicit effect kinds: execution starts/seconds include `cursor.run_turn`,
`codex.implementer_bootstrap`, `codex.implementer_resume`,
`codex.bootstrap_review`, `codex.resume_review`; exclude `cursor.create_chat`,
preflight, probes, staging/ingestion, fake and unknown effects. Do not use the
broader CURSOR_ATTEMPT_EFFECT_KINDS set unchanged: it includes chat creation.
Missing/unknown effect ownership is reported in coverage and excluded from
execution metrics. Timeline inspection may still display those other attempts,
with clear labels; their existence does not make a run count as executed.
Codex implementer attempts have component `codex_implementer`; Codex reviewer
attempts have component `codex` and a review effect. Derive role/provider from
these authoritative component/effect pairs, never from the word Codex, model
name, or iteration. Distinct implementer/reviewer sessions remain private and
cannot be merged for counting. Preserve current implementerProvider in map/
selected run data and default only absent historical provider to Cursor. Preserve
pending role/turn/capacity and implementer retry wait/counters from current inspect
DTOs, without interpreting a displayed safe action as scheduler authority.

**Started cohort:** a run's `firstExecutionLaunchAt` is the minimum valid
launchRequestedAt over its owned execution attempts. It is an observed launch
request, not a guarantee of OS execution or the scheduler start transition.
No qualifying launch means no membership in the started cohort; submitted-only
runs and missing historical timestamps are counted separately in coverage.
Runs whose first execution launch is in range supply map, KPI, bars and workload.
Use validated states; completed_with_residual_risk is separate from completed.
Preserve unknown/new states as Other with their raw value. State groups are
completed, residual-risk, running, waiting, blocked, aborted, other, with a
explicit mapping: completed -> completed; completed_with_residual_risk ->
residual-risk; preflight_complete/cursor_ready/awaiting_codex_review/
waiting_for_cursor_fix/checkpoint_pending -> running; queued/authorized/admitted/
waiting_usage_limit/waiting_codex_capacity/waiting_codex_review_retry/
pending_sequence_review_recovery -> waiting; blocked/max_iterations_reached ->
blocked; aborted -> aborted; any other raw state -> other. Waiting/blocked
KPI is the sum of its two groups, not a guess from safe-action text.

**Submissions:** count each materialized run once by submittedAt in range,
including queued runs; sequence preparation is not a run submission.

**Review rounds:** count each `(runId, iteration)` once, dated at its first valid
Codex reviewer launch (`codex.bootstrap_review` or `codex.resume_review`,
component `codex`). Codex implementer turns never count as review rounds.
Multiple phaseAttempt retries in one iteration are one round.
Count launched rounds regardless of their eventual outcome; this is distinct
from authenticated completed review-budget counters. Separate successor runs
remain distinct run IDs, including same-reviewer recovery; do not deduplicate
cross-run iterations by reviewer session/name. Expose this definition in the UI.

**Process hours:** include owned, resolved execution attempts with valid start/end,
completedAt >= launchRequestedAt and public observed duration present. Canceled intervals additionally require
existing ingested/reconciled termination evidence; a cancellation intent alone
is not a resolved process. Use the
existing duration projection, accepting resolved failed/cancelled attempts when
it supplies an interval, and exclude active/uncertain/missing timestamps. Clip
intervals to the requested range and split at local calendar midnights using IANA
zone rules (including 23/25-hour days). Sum parallel intervals as process seconds;
two simultaneous one-hour attempts contribute two hours. Display seconds / 3600;
round for display only. Never count capacity waits, submission-to-finish span,
CPU usage or invent ongoing/historical duration. Hours may have incomplete
coverage even when every relevant stored row has been read.

Activity uses event/interval dates across all project-matching runs, including runs
first launched before the range. It deliberately differs from the started cohort.
Days without activity are zero only when available evidence was fully read. Return
`coverage` metadata: `dataState` complete/partial/empty, known exclusion counts
(unstartedRuns, missingLaunchTimes, unknownExecutionKinds, missingDurationAttempts),
and allowlisted warning codes. Coverage exclusions describe project-matching
stored evidence; runs without a launch cannot be assigned to a date range.
Known queued/unstarted runs and deliberately excluded ongoing intervals are
normal exclusions, not proof of lost history; partial is reserved for missing
required historical evidence. Corrupt authoritative data fails; partial never
means arbitrary truncation or guessed success. In a partial activity series,
nonzero values/total are labelled recorded contributions (lower bounds), zero
days are omitted and the UI marks absent days unknown. Only complete/empty
coverage yields explicit zero days. Do not interpret missing launch as no work.

## Public Integration API: Phase 26.1 additions

On the refreshed planning baseline API is 1.8 and scheduler schema is 16.
Add compatible minor 1.9 and capability
`dashboard` (strict bool, absent defaults false). All four handlers below must
exist before advertising true. Recheck once at admission; if an intervening
accepted change already uses minor 1.9, use the next additive minor consistently
and record it rather than downgrade. Major-only gates and existing commands stay
compatible; lack of dashboard capability prevents future consumers dispatching it.

| New CLI | Query | Result data |
| --- | --- | --- |
| `integration dashboard projects --offset N --limit N` | Local installation project catalog, all retained materialized runs | `items[{projectName, runCount}]`, `page`, `totalCount`; sorted projectName, no slug deduction. |
| `integration dashboard summary --from UTC --to UTC [--project NAME] [--time-zone IANA]` | Started cohort | `filters`, `coverage`, `totals{runsStarted,completed,residualRisk,waitingBlocked}`, `projects[{projectName,total,stateCounts}]`, `workload[{projectName,sequenceId|null,sequenceName|null,runCount}]`. |
| `integration dashboard runs --from UTC --to UTC [--project NAME] [--time-zone IANA] [--offset N --limit N]` | Started cohort map page | `filters`, `coverage`, `items[{runId,projectName,implementerProvider,state,submittedAt,firstExecutionLaunchAt,sequenceBinding|null}]`, `page`, `totalCount`; binding contains sequenceId/name, ordinal/phaseName, generation, attemptKind and sourceRunId when authoritatively recorded. |
| `integration dashboard activity --from UTC --to UTC [--project NAME] [--time-zone IANA] --metric submissions|review-rounds|process-hours` | Event-date activity | `filters`, `coverage`, `metric`, `unit` count/process-seconds, chronological `days[{date,value}]`, `total`; seconds stay numeric and finite. |

All use existing success/failure envelopes, exit codes and `--output json`.
Summary/run IDs are public resources, not full agent session IDs. New schemas
`integration-api-dashboard-{projects,summary,runs,activity}-data-v1.json` plus
shared dashboard schema are packaged and registered. Semantic metric enums are
closed; scheduler display states remain open strings. Public known fields have
explicit absent/null semantics; do not alias extra='allow' internals wholesale.

The four operations plus existing run.inspect/timeline, sequence.inspect/phase-runs,
Codex capacity and explicit frozen/review/output readers supply the complete UI.
No new generic history dump is needed. Semantic aliases are
`dashboard.projects`, `dashboard.summary`, `dashboard.runs`, `dashboard.activity`.

## Local HTTP and CLI: Phase 26.2 additions

`ai_dev_loop dashboard serve [--port PORT]` binds exactly 127.0.0.1, default 8765;
port is strict 1..65535. No host/bind escape, browser auto-open, daemon, timer,
remote deployment, write route or installation side effect. Default storage uses
the existing source-user XDG resolver. Tests select isolated XDG in subprocess
environments; public HTTP clients cannot select db/filesystem paths.

Use Python standard-library BaseHTTPRequestHandler / HTTPServer with a bounded
worker adapter (four active handlers, no unbounded ThreadingMixIn admission).
HTTP is deliberately local; do not present this as a public production server.
Serve only a package-owned static asset manifest, no directory listing or arbitrary
filesystem roots. In 26.2 `/` honestly describes the frontend as pending; 26.3
replaces it with the built UI. Future wheel assets are same-origin and self-contained.

`POST /api/dashboard/read` body `{operation, parameters}` is the sole read
adapter. Allow exactly four new dashboard operations; existing `run.inspect`,
`run.timeline`, `sequence.inspect`, `sequence.phase-runs`, `machine.codex-capacity`,
`run.initial-prompt`, `run.review`, `run.review-content`, `run.output` use their
existing typed services and DTOs. Content kind/stream remains explicit. No generic
command/path/argv/SQL string is accepted. Runtime methods are read-only regardless
of using POST. Preserve stdout/stderr chunk limit default/max 65,536 bytes for this
HTTP adapter; frozen/review chunks default 65,536, max 65,536. JSON request <=16 KiB,
no streaming request body, total handler deadline <=30 seconds, socket I/O <=5 s,
<=2 admitted service jobs, with immediate 503 safe saturation and no permanent
queue. Capacity probe is separately single-flight and bounded by its existing
service budget; other reads do not probe Codex.

Require exact Host `127.0.0.1:<bound-port>` and exact Origin
`http://127.0.0.1:<bound-port>` for API POST, including explicit content requests.
Serve UI GETs with the same Host check. Reject foreign/null/missing Origin API
requests and unsupported methods before dispatch; no CORS wildcard, proxy trust,
credential or login flow. Client requests are JSON and browser-generated Origin.
Use no-store for API responses, safe fixed error envelopes/statuses (200 success; 400 INVALID_ARGUMENT;
404 NOT_FOUND; 409 UNSUPPORTED/DATA_INTEGRITY; 500 INTERNAL_ERROR/IO_ERROR;
503 saturation; 405 method; 403 Host/Origin; 413 request size; reuse
existing safe error codes, with INTERNAL_ERROR for saturation and INVALID_ARGUMENT
for method/origin/size rejections), nosniff,
referrer-policy no-referrer and same-origin CSP compatible with the chosen ECharts
renderer (script-src self; style-src self with inline presentation styles allowed;
object-src none; base-uri none; no unsafe-eval). Bound envelopes, not static
JavaScript asset size, to 240 KiB; manifest-owned static files are <=10 MiB each.
Bodies/parameters/content never enter access logs or trace/screenshot recordings.
Display receivedAt/source=local in local client metadata without mislabeling it
as a remote live/cache wrapper. Ctrl-C/restart closes owned sockets/jobs and keeps
ledger/artifacts unchanged; no auto-launch on package import.

## Frontend, inspection and refresh policy

Use React 18, TypeScript, Vite, ECharts ^6 and echarts-for-react ^3, matching the
Hub's intended chart stack. Pin tested dependency versions and lockfile in 26.3;
Node is development/build-only. Bundled assets must run without CDN/network/model
access. Official reference: [ECharts imports](https://echarts.apache.org/handbook/en/basics/import/).
Import/register only required chart/render modules where supported. Use light/dark
system-font presentation from the reference; theme preference is the only browser
storage. Components and filters use memory, never persist summaries/content.

Initial filter: all projects, last seven local calendar days including today
(start at local midnight six days ago; upper bound at current time), named default
zone, submissions activity. Controls support presets 7/30/90 days and custom local
From/Until; show exclusive upper boundary and selected zone. Apply commits the
filter, while invalid draft inputs keep prior results. Changing project/range retires
in-flight requests, later pages, selected run/attempt and open content; request
generation prevents old replies reappearing. No guessed fallback selection.

Widgets: four KPIs; stacked horizontal bars by project/state; workload treemap
project -> authoritative sequence / standalone; current capacity per returned
window; sequence-and-run map with independent runs and all replacement generations;
custom Gantt attempt timeline; selectable-metric activity calendar. Full map data
uses explicit paging/load-more and visible coverage; totals do not rely on loaded
map rows. Keyboard-selectable companion rows/buttons provide chart-equivalent
navigation. No color-only statuses or HTML rendering of source strings.

Map selection loads the exact run timeline and, when needed, authoritative
sequence detail. Existing attempts/timeline paging is retained; gap/no-end bars
never imply a complete duration. Clicking one attempt opens its metadata modal.
Opening the modal requests no content. Buttons deliberately load prompt/stdout/
stderr; content responses stay memory-only, paginated, text-rendered, and have
loading/absent/changed/error/active/truncated states. Close/reselect/filter/route
retirement aborts requests, increments generation and releases buffered bytes.
Content is not polled and never enters summary DTOs or screenshots.

Reviewer prompt is exact per attempt when public review evidence exists and the
selected component/effect identifies a reviewer. An initial frozen run prompt is
labelled run context for both Cursor and Codex implementers, not the exact captured
execution envelope. Codex implementer stdout/stderr already use run.output with
provider-specific evidence authentication; never send an implementer attempt to
run.review/review-content. Exact implementer attempt prompts (including corrections)
are unavailable without an authoritative public per-attempt binding;
do not guess the preceding review ID or browse files to invent that link. Existing
review cursor-fix-prompt can be explicitly inspected for its own review attempt,
with that ownership visible. Missing historical prompt remains not_recorded.

Automatic summary/map/activity refresh targets 30 s start-to-start, selected run
inspect/timeline 10 s, capacity 60 s. These are minimum cadences, not latency
promises. A global client queue allows two active reads, no overlapping identical
key, no catch-up, bounded request timeout. Only visible widgets/pages are eligible.
Pause, hidden document, route leave and last widget retirement stop polling and
cancel owned queued work; explicit Refresh is still available while paused.
Refresh updates currently displayed pages, never silently scans every historic page
or loads sensitive content. All pages reset on explicit dataset Refresh. Failures
retain only in-memory prior data labelled last successful observation and the
error; partial/new selection never inherits old filter results. There is no Hub
persistent cache, browser WSS, shared multi-tab polling service or service worker.

## Observable completion and documentation

Each plan implements its listed contracts through production callers and focused
independent tests, with exact evidence. Final 26.7 owns installed wheel/browser
acceptance, the single repository-wide integration gate and a human walkthrough.
Use fake providers, controlled temporary ledger/artifacts and independent expected
metrics. An optional manual source-account observation is read-only and may call
the existing capacity probe, but cannot start/abort/tick/retry scheduler work.
No provider implementation/review turns are acceptance prerequisites.

Root ai_dev_loop.yaml, review/correction schemas, planning/review skills, Phase
24/25 plans and runtime drivers are unchanged. Use a stable installed execution
driver while the self-hosted source is changing. Planning does not prepare/start
this sequence, commit/push, install a package, activate services or authorize
human acceptance. The operator explicitly authorized this replacement preparation/start on
2026-10-15; its source baseline is independent of Hub Phase 15 acceptance. This
launch does not declare Hub manual acceptance or modify the Hub worktree.

## Phase 27 execution baseline

Use manifest v2 with per-phase implementer provider=codex, command=codex,
model=gpt-5.6-sol, reasoning_effort=xhigh, timeout_minutes=90. Keep reviewers
independent: gpt-6.1-sol xhigh for 26.1/26.2/26.7 and high for 26.3–26.6, with
90-minute reviewer timeout and seven reviews. Do not include phase cursor fields
or workflow.cursor_timeout_minutes with the Codex provider. Root YAML remains
at its supported defaults; every selected implementer value is in the manifest.
Phase 27.1–27.7 production is implemented; Phase 24/25 remain unexecuted planning.
Use accepted role-aware readers, output resolution and implementer_retry lineage.
Do not introduce SDK/token dependencies or change recovery/state transitions.
Current Phase 27 findings retain historical full-suite/build verification limits;
26.7 owns the Dashboard integration gate. The known active-attempt/status-action
incident remains upstream inspection context: expose raw checkpoint plus observed
attempt status independently, not a fabricated scheduler state or retry button.
