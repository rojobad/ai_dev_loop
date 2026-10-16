# Phase 26 local dashboard to future Hub handoff

Status: current local producer/consumer contract recorded for future planning.
Nothing in this handoff implements or authorizes Hub, Bridge, Live transport,
remote authentication, public deployment or a durable dashboard cache.

## Accepted local surface

The installed producer is Integration API **1.9**, capability
`dashboard=true`, over accepted scheduler SQLite schema **16**. The public
dashboard schemas packaged in the wheel are:

- `integration-api-dashboard-shared-v1.json`
- `integration-api-dashboard-projects-data-v1.json`
- `integration-api-dashboard-summary-data-v1.json`
- `integration-api-dashboard-runs-data-v1.json`
- `integration-api-dashboard-activity-data-v1.json`

The semantic operations are `dashboard.projects`, `dashboard.summary`,
`dashboard.runs` and `dashboard.activity`. The complete UI also consumes the
existing typed `run.inspect`, `run.timeline`, `sequence.inspect`,
`sequence.phase-runs`, `machine.codex-capacity`, `run.initial-prompt`,
`run.review`, `run.review-content` and `run.output` operations. Public Python
DTOs are explicit camelCase projections; they are not database rows, scheduler
domain models or ECharts options.

The local HTTP adapter accepts `{operation,parameters}` only at
`POST /api/dashboard/read`, bound to `127.0.0.1` with exact Host/Origin. It is a
local transport, not the future Hub protocol.

## Semantic contract to preserve

- Filters normalize to required UTC `[fromUtc,toUtc)`, named IANA `timeZone`
  and exact nullable `projectName`; range is positive and at most 366 days.
- Summary/runs use the run's first owned execution launch. Chat creation,
  preflight and probes do not start the cohort. Submitted-only evidence is
  coverage, not an invented launch.
- Project/provider/sequence lineage comes from authoritative fields. A missing
  historical provider defaults to Cursor. `implementer_retry` and its source
  are explicit lineage and are never parsed from run IDs.
- Executions are `cursor.run_turn`, `codex.implementer_bootstrap`,
  `codex.implementer_resume`, `codex.bootstrap_review` and
  `codex.resume_review`. Only the last two, with component `codex`, are reviewer
  rounds. Component `codex_implementer` is never reviewer content.
- Submissions are dated by `submittedAt`; review rounds count once per
  `(runId,iteration)` at first reviewer launch; process-hours sum resolved owned
  intervals, split at named-zone midnight, including parallel time. Activity is
  event/interval based and intentionally differs from the started cohort.
- `coverage` explicitly distinguishes complete/partial/empty, missing launches,
  unknown execution kinds and missing duration evidence. No consumer may turn
  missing/active/uncertain evidence into zero duration or success.
- Every operation is a separately dated observation. Pages are fresh snapshots;
  `totalCount` belongs to its page query, not a cross-page transaction.

Bounds are part of the contract: page limit 100; 10,000 relevant runs; 100,000
contributing attempts; five-second ledger budget; 240 KiB response envelope;
65,536-byte content chunks. A bound failure returns a typed error with no partial
totals. Content is deliberate and paginated; historical absence remains typed
unavailability such as `not_recorded`. There is no public exact per-attempt
implementer-prompt operation.

## Reusable frontend boundary

Components depend on the `DashboardDataSource` interface in
`dashboard-web/src/dashboard/types.ts`. `LocalFetchDashboardDataSource` is the
only localhost/fetch adapter; strict parsers validate the API-major-compatible
DTOs before components receive them. Chart components own presentation and
ECharts configuration, not transport or scheduler semantics.

The refresh coordinator owns a maximum of two client reads, canonical keys,
timeouts, cancellation and response generations. A future adapter must preserve
scope/selection retirement: late results cannot restore old map pages, timeline
selection or sensitive content. Prompt/review/output remains memory-only and
deliberate; only the theme is durable browser state.

Installed assets are the manifest-owned `index.html`, `assets/dashboard.css`
and `assets/dashboard.js` under `ai_dev_loop.dashboard.web_dist`. The wheel is
self-contained and same-origin; the sdist carries pinned frontend sources,
lockfile and build tooling. Missing/stale assets fail package construction.

## Additional future work — not implemented

A future Hub phase must make and review its own decisions for all of the
following before reusing this UI:

1. Add typed Bridge handlers for the operations/DTOs above, including machine
   selection and capability negotiation; do not proxy generic SQL, filesystem
   paths, argv or scheduler commands.
2. Add authenticated/authorized Hub HTTP routes and machine tenancy rules. The
   local no-login loopback security model cannot be exposed remotely.
3. Implement a Hub `DashboardDataSource` adapter that maps the remote transport
   to the same DTOs and preserves API-major/capability checks, cancellation,
   limits and typed unavailable/error outcomes.
4. Define explicit allowlists and privacy policy for frozen prompt, review and
   process-output reads. Machine/run/attempt ownership must be authenticated;
   implementer attempts must remain excluded from reviewer-content calls.
5. If Live updates are desired, specify subscription ownership, backpressure,
   reconnect/generation fencing and visibility/Pause behavior. No browser WSS or
   Live protocol exists today.
6. If caching is desired, define keys including machine, canonical filters,
   operation, pagination and authorization; preserve `observedAt`, coverage and
   content sensitivity. No Hub cache, service worker or durable local result
   cache exists today.
7. Add independent Bridge/Hub contract, integration, browser, privacy and
   operational acceptance. Local installed-wheel acceptance is evidence for the
   shared semantics only, not remote production readiness.

Phase 26 does not change Hub/Bridge source, deploy a service, choose remote auth,
or claim Hub Phase 15/manual acceptance. Those remain separate future scope.
