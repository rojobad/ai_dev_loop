# Phase 26.2 loopback read server findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, commit, or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-26-local-dashboard`, starting HEAD
`161e6eb0a4b2f0d66be35028580d40dc94d2c017`. Initial Git status was clean.
Implementation used the accepted Phase 26.1 / Phase 27 production baseline:
Integration API 1.9, SQLite schema 16, provider-aware inspection/output and
authoritative `implementer_retry` lineage. Phase 24 and Phase 25 were not used as
dependencies.

The implementation adds the foreground `dashboard serve --port` CLI, a bounded
IPv4 loopback HTTP server, an explicit typed operation dispatcher over the
existing read services, and one provisional package asset. The server has no
login, remote-bind option, CORS mode, daemon behavior, generic filesystem/SQL/
command dispatch or scheduler mutation. Sensitive reads keep the accepted
run/attempt ownership, pagination and evidence authentication.

No scheduler run was submitted, started, ticked, retried, extended, aborted or
otherwise controlled during this implementation. No real Cursor/Codex model turn,
capacity probe, package installation, timer change, root-YAML change, external Git
action, commit, push, PR or merge was performed. Automated provider
coverage used only the repository fake executables and temporary owned resources.

## Admission and prerequisite evidence

The accepted prerequisite was checked once before dependent implementation.
Source inspection established the four Phase 26.1 service/CLI entry points, API
1.9, schema 16, all five execution effects, reviewer-only review content,
provider-specific process-output authentication, historical Cursor defaults,
current role/retry fields and sequence replacement lineage. The one-time test was:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/integration/test_phase26_1_dashboard_queries.py tests/integration/test_phase27_3_inspection.py tests/unit/scheduler/test_schema_readonly_historical.py
```

Result: **exit 0, 29 passed in 29.52s** (29.84s tool wall). Dependent work began
only after this pass. The selection independently covers Codex implementer
execution/start/time ownership, reviewer-only rounds/content, provider/role
privacy and authenticated implementer output. No prerequisite was missing or
incompatible and no execution-time OpenQuestion was created.

## Contract evidence

| ID | Production entry and caller | Independent test/assertion | Exact result |
| --- | --- | --- | --- |
| C262-1 | `cli.app` -> `commands.dashboard.dashboard_serve_command` -> `DashboardServer` -> `BoundedDashboardHTTPServer` | `test_c262_1_real_cli_loopback_sigint_restart_and_no_storage_mutation` launches the real CLI twice on an owned port with copied HOME/XDG, waits through real HTTP, performs a read, sends SIGINT and restarts. It asserts an invalid/out-of-range port, non-integer port and occupied port fail without creating the isolated state root; help exposes no remote host option; primary ledger bytes, artifact bytes/modes and scheduler run rows remain unchanged. | Included in final mandatory selection: **52 passed in 15.09s**. |
| C262-2 | `DashboardRequestHandler` -> `DashboardHttpDispatcher.prepare` -> lazy existing dashboard/run/sequence/review/output/capacity services -> shared success/failure envelope | `test_c262_2_allowlisted_dispatch_uses_one_typed_service` covers every operation and proves validation precedes service construction. Policy tests reject bad method/Host/Origin/size without dispatch. `test_c262_2_http_and_cli_share_dashboard_semantics` compares real CLI and HTTP summary data modulo observation timestamps. The fake-provider sequence test reads owned Cursor and Codex implementer output, reviewer detail/content, provider fields, Codex implementer/reviewer timeline rows and mixed-provider sequence DTOs; wrong run/attempt ownership fails and a Codex implementer attempt is rejected by reviewer-content. Existing required auth regressions independently verify process/review evidence. | Included in final mandatory selection: **52 passed in 15.09s**. |
| C262-3 | `DashboardRequestHandler.setup` / `_DeadlineReader` / `_send_bytes` and `BoundedDashboardHTTPServer.process_request` / `run_service` -> one absolute request deadline, maximum four handler threads, two read jobs and one capacity probe -> owned shutdown | `test_c262_3_service_and_handler_admission_timeout_then_recovery` synchronizes the real `IntegrationDashboardReadService` on its read port, fills two service/four handler slots, observes immediate 503, expires the request deadline and proves later recovery. `test_c262_3_absolute_deadline_retires_four_trickled_handlers_and_recovers` occupies all four slots separately at request-line, header and POST-body reception, continues fragments inside the idle timeout, and asserts every handler retires in under 0.8s, dispatch count remains zero, permits recover and a later normal read succeeds. `test_c262_3_expired_and_late_started_work_never_executes_and_permits_recover` covers ordinary and capacity-probe admission both when already expired and when thread start is delayed across the deadline, asserting zero executions and successful permit reuse. Separate retained tests cover disconnect, service timeout, capacity single-flight and a real schema-16 SQLite deadline. | Focused server-policy file: **10 passed in 3.35s**. Included in final mandatory selection: **52 passed in 15.09s**. |
| C262-4 | fixed `DEFAULT_STATIC_MANIFEST` / package `web_dist/index.html` -> `DashboardRequestHandler` response adapter -> HTTP client | `test_c262_4_manifest_headers_traversal_and_safe_internal_error` reads the honest English provisional page and asserts `no-store`, nosniff, no-referrer, same-origin CSP without unsafe-eval and no CORS. Unlisted, lexical/encoded traversal and query targets are unavailable. An injected private-path sentinel in an upstream exception is absent from the fixed error envelope; handler logging is disabled. Unknown standard-library methods are converted to the same fixed JSON policy rather than diagnostic HTML. Oversize response data is replaced with a bounded safe error. | Included in final mandatory selection: **52 passed in 15.09s**. |

## Implemented boundaries

- `dashboard serve` validates port 1..65535 and binds exactly `127.0.0.1`; bind
  occurs before any lazy service/XDG resolution. It serves only `/` and
  `/index.html` from a fixed package resource plus the single typed POST route.
- API requests require the exact bound Host and Origin, one JSON body no larger
  than 16 KiB, no transfer streaming, and a closed allowlist of operation names.
  Collection pages default to 25/max 100; explicit content chunks default/max to
  65,536 bytes. Success envelopes are capped at 240 KiB and static assets at
  10 MiB.
- Every response is connection-closing and no-store with nosniff, no-referrer and
  same-origin CSP. Request bodies, parameters, query strings, private paths and
  upstream diagnostics never enter handler logs. Unexpected errors become fixed
  `INTERNAL_ERROR` envelopes.
- Handler admission happens before thread creation. Service jobs have no queue:
  they acquire one of two permits before their owned daemon thread is started and
  retain it through completion even if a client disconnects or the handler
  deadline expires. Capacity additionally acquires a one-flight permit. Stop
  closes owned sockets and joins bounded owned handlers/jobs; it never signals a
  scheduler/provider attempt.
- The request-line, header and fixed-length body reader recalculates the remaining
  monotonic request budget before every raw socket receive, so arriving fragments
  cannot renew the absolute deadline. Response headers and bodies are likewise
  bounded by the same deadline. Service admission checks expiry before permits
  and worker start, and the worker checks again immediately before invoking
  either an ordinary read or capacity probe.
- Each operation constructs the accepted service lazily and each service uses the
  accepted read-only store with one connection per operation/transaction. The HTTP
  layer opens no enclosing SQLite transaction across service, artifact/probe or
  response send. SQLite can maintain its ordinary read-only WAL shared-memory
  sidecars; the asserted authority boundary is unchanged `engine.sqlite3` bytes,
  unchanged artifact contents/modes and unchanged scheduler rows.
- `run.review` and `run.review-content` still accept only `component=codex` plus
  the review-effect allowlist. `run.output` retains provider-specific authentication
  for Cursor, Codex implementer and reviewer attempts. No implementer prompt is
  reconstructed. Returned sensitive bytes exist only in the bounded response and
  are not cached or logged by the server.

## Exact focused validation

The required commands used an owned temporary uv cache because the sandbox home
cache is read-only.

F-01 deadline/admission regression selection:

```bash
env -u AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT -u AI_DEV_LOOP_REVIEW_PROMPT_REL -u AI_DEV_LOOP_REVIEW_EVIDENCE_REL TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/unit/dashboard/test_server_policy.py
```

Result: **exit 0, 10 passed in 3.35s** (4.05s tool wall). This retains the
existing service-timeout, disconnect and capacity single-flight tests while
adding three four-slot real-socket trickle cases plus ordinary/capacity expired
worker assertions.

Mandatory Phase 26.2 and affected authentication selection, isolated from the
automated review environment's inherited prompt-witness variables:

```bash
env -u AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT -u AI_DEV_LOOP_REVIEW_PROMPT_REL -u AI_DEV_LOOP_REVIEW_EVIDENCE_REL TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m pytest -q tests/unit/dashboard tests/integration/test_phase26_2_dashboard_server.py tests/unit/integration_api/test_process_output_auth.py tests/unit/integration_api/test_review_evidence_auth.py
```

Final result: **exit 0, 52 passed in 15.09s** (15.42s tool wall).

Static contract checks:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m ruff check src/ai_dev_loop/dashboard/adapters/http.py src/ai_dev_loop/dashboard/server.py tests/unit/dashboard/test_server_policy.py
TMPDIR=/tmp TMP=/tmp TEMP=/tmp UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run python -m mypy src
```

Final results: **both exit 0** in one chained invocation (0.69s tool wall).
Ruff reports `All checks passed!`; MyPy reports no issues in **202 source files**.

Documentation build:

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-uv-cache uv run mkdocs build --strict
```

Final result: **exit 0**, documentation built in **0.72s** (0.94s tool wall).
Output contains the upstream Material-for-MkDocs informational banner and the
existing incident-pages-outside-nav notice; neither is a strict-build failure.
The requested docs governance skill kept the new text limited to the implemented
CLI/HTTP/read/privacy facts.

Repository whitespace:

```bash
git diff --check
```

Final result: **exit 0** (0.00001s tool wall), including this findings file.

### Corrected development failures

- The first F-01 static invocation stopped after Ruff reported one `SIM105` in
  the new test trickler (**exit 1**, 0.20s tool wall); it was corrected to use
  `contextlib.suppress`. The subsequent exact Ruff and MyPy commands both pass.

- Initial new unit selection: **exit 1, 1 failed / 23 passed in 1.29s** because
  an expired handler had no response-send reserve and closed the connection. The
  server now reserves bounded response time; the corrected selection passed
  **24 tests in 0.97s**.
- Initial new integration selection: **exit 1, 2 failed / 1 passed in 8.16s**.
  One assertion confused project count with run count, and raw fake Cursor bytes
  deliberately made its JSON stream unparsable. The independent expectations
  were corrected without weakening production; a second run exposed only a
  TIME_WAIT-specific raw bind assertion (**1 failed / 2 passed in 9.40s**), which
  was replaced by the actual supported CLI restart. The corrected file passed
  **3 tests in 11.44s**.
- After adding explicit real-reader disconnect/query-budget and capacity tests, a
  targeted combined selection passed **8 tests in 13.82s**. The first isolated
  capacity-enhanced server-policy run exposed a nondeterministic assertion before
  the second worker reached its synchronization point (**1 failed / 4 passed in
  1.03s**); waiting on the production active-service counter fixed the test, and
  the corrected file passed **5 tests in 1.65s**.
- Early static checks found eight Ruff mechanics, then one unused import and two
  MyPy errors (one redundant cast and one base-server override type). All were
  corrected before the final clean commands. No failed mandatory behavior is
  hidden by a narrower successful rerun.

## Preserved surfaces and unfinished work

The requested protected inputs retain their prior SHA-256 values:

- `.agents/skills/create-cursor-plan/SKILL.md`:
  `6784d767cb8de6344ec8d431f8312fe71be6dbefd93c467e9bb772529b547a6a`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`:
  `7563c117439618708906ac9c661506a58b8b90a1f3726b5a55be4a26aac73b4c`
- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`:
  `7e090d56f571852d7687c2d33e3725f44977102abeca182e1248bc610ffd71c3`
- `ai_dev_loop.yaml`:
  `33cd5c07ca3eafb640200df788ee7027eee728192d1e1fa9521d5a45dbe9f187`

F-01 is **FIXED** in `src/ai_dev_loop/dashboard/adapters/http.py` and
`src/ai_dev_loop/dashboard/server.py`; the independent assertions are in
`tests/unit/dashboard/test_server_policy.py`. No mandatory C262 behavior or
focused automated test is unfinished. The
repository-wide pytest suite, built-wheel/isolated installed-runtime check,
React/browser UI, real capacity/model calls, live workstation source observation
and operator walkthrough were deliberately **not executed**. The approved plans
assign frontend work to 26.3-26.6 and the single repository-wide/package/human
acceptance gate to Phase 26.7. Green focused checks do not authorize human
acceptance, final commit or deployment.
