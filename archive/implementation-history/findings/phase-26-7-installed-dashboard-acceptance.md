# Phase 26.7 installed dashboard acceptance findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, final commit or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-26-local-dashboard`, starting HEAD `a6267f5d6661`. The accepted
Phase 26.1-26.6 production and Phase 27.1-27.7 provider-aware baseline were used:
Integration API 1.9, SQLite schema 16, historical absent provider as Cursor,
current pending-role/retry metadata and authoritative `implementer_retry`
lineage. Phase 24 and Phase 25 were not dependencies. No OpenQuestion was open
and no prerequisite incompatibility was found.

This subphase adds deterministic frontend build provenance and a package-build
guard, ships the complete same-origin frontend plus five dashboard schemas,
proves a wheel-installed CLI outside the checkout without Node on runtime PATH,
and provides an owned acceptance/prepare/cleanup harness. Its fixture uses only
temporary XDG/Git state and fake providers. It contains historical Cursor plus
current Codex implementer runs, a forced `implementer_retry` relationship,
reviewer attempts, more than one map/timeline page, unavailable historical
review evidence and active process output.

The installed browser journey covers overview, workload, current capacity, map,
timeline, attempt detail and all activity metrics. Codex implementer attempts
contribute execution start/duration/timeline but never review-round counts or
reviewer-content calls. Content remains deliberate, paginated and memory-only;
scope/selection retirement, Pause/Refresh/visibility ownership and local-only
network behavior are asserted. The scheduler SQLite database and protected
artifact files are hashed before and after reads and remain unchanged.

Current Spanish operator/reference docs, a manual walkthrough and an exact
future Hub handoff were added. The handoff records the actual API 1.9 DTO and
schema surface while explicitly leaving Bridge, Hub HTTP/auth, Live and cache
work unimplemented and unauthorized.

No real provider turn, active scheduler control, operator installation, timer,
root YAML, reviewer schema, review skill, reservation, external Git action or
live ledger/artifact mutation occurred. The source checkout was not used as the
live implementation scheduler driver.

## Admission and prerequisite evidence

The accepted prerequisites were checked once before dependent implementation.
Source inspection confirmed the Phase 27 component/effect identities and Phase
26.1-26.6 typed producers/consumers, including API 1.9 registration, SQLite
schema 16, read-only services, content authentication and refresh ownership.
Earlier findings had no unfinished mandatory predecessor item; the historical
26.3 issue was closed by accepted 26.4 production.

The one supplemental Python admission command selected the shared dashboard
query/server/content and provider-aware inspection contracts:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp \
UV_CACHE_DIR=/tmp/ai-dev-loop-phase267-admission-uv-cache \
uv run python -m pytest -q \
  tests/integration/test_phase26_1_dashboard_queries.py \
  tests/integration/test_phase26_2_dashboard_server.py \
  tests/integration/test_phase26_5_attempt_content.py \
  tests/integration/test_phase27_3_inspection.py \
  tests/unit/scheduler/test_schema_readonly_historical.py
```

Result: **34 passed in 58.98s, exit 0**. The frontend admission typecheck also
passed. The first complete Vitest admission exposed an accepted-test gap rather
than a producer incompatibility: `DashboardChart.test.tsx` did not mock the
already accepted ECharts `HeatmapChart`, `CalendarComponent` and
`VisualMapComponent`; **11 files / 53 tests passed and 1 suite failed during
collection**. After adding those three test-only exports, **12 files / 54 tests
passed in 3.69s**. No admission suite was repeated after dependent work began.

The original admission used Python 3.11.15 as both the project development
runtime and isolated wheel runtime. Correction review then identified and used
the separately installed Python 3.13.14 as the required supported newer-runtime
case. `/usr/bin/python3` is 3.10.12 and remains below the declared `>=3.11`
support boundary.

## Contract evidence

| ID | Production entry and caller | Independent test/assertion | Exact executed result |
| --- | --- | --- | --- |
| C267-1 | `dashboard-web` production inputs -> `write-build-manifest.mjs` -> `web_dist` -> Hatch `CustomBuildHook` -> wheel/sdist -> isolated venv `ai_dev_loop dashboard serve` -> allowlisted HTTP reads -> `LocalFetchDashboardDataSource`/strict parsers -> dashboard components. The installed PATH contains the wheel venv, fake provider bin and system utilities but no Node; cwd is outside the checkout and `PYTHONPATH` is absent. | `test_c267_1_and_c267_3_installed_acceptance_real_server_browser_and_package` invokes the real package harness for Python 3.11 and a supported newer runtime. `installed-acceptance.spec.ts` blocks non-loopback HTTP; asserts exact independent activity totals/day buckets, including **14,400 process seconds = 4 displayed hours** and one excluded active duration; asserts 27 runs, both providers, 3 review rounds, second map/timeline pages, exact `implementer_retry` lineage, deliberate initial prompt/stdout/stderr, `not_recorded` historical review prompt, exact later reviewer prompt, active-output EOF polling, zero reviewer-content request for an implementer, Pause/manual Refresh/hidden behavior and theme-only storage. Package inspection independently requires the manifest/assets/five schemas, frontend sources/lock/build tooling, isolated `-I` import origin and absence of CDN/source maps/runtime captures. | Original final Phase 26.7 file: **6 passed in 64.37s / 65.31s elapsed, exit 0**. Corrected runtime/tooling matrix: **9 passed in 98.15s / 98.26s elapsed, exit 0**, including one full installed journey each under **Python 3.11.15** and **Python 3.13.14**. Final frontend unit suite: **12 files / 54 tests passed in 4.50s / 5.25s elapsed**. Existing real-server browser regression sweep: **11 passed in 30.7s / 31.98s elapsed**. |
| C267-2 | `verify-phase26-dashboard.py` inventory/case dispatcher and accept/prepare/cleanup -> private `/tmp/ai-dev-loop-phase26-dashboard-*` resource, installed venv, fake fixture, loopback process, `.owner.json` and explicit registry -> identity-checked termination/removal. | `test_c267_2_list_prepare_usable_and_exact_cleanup` requires the nonempty four-case inventory, `MANUAL_PENDING`, a live usable installed site, then exact cleanup. Parameterized tests create token conflict, PID-start uncertainty and a symlinked resource-root conflict and prove the sentinel/registry survive with `resources were preserved`. The harness checks uid/token/non-symlink root and owner record/PID start/executable argv/process group and refuses ambiguous cleanup. | Complete corrected focused snapshot: **9/9 passed in 98.15s / 98.26s elapsed**, including one usable prepare/cleanup and all **3/3** preservation cases; the runtime parameter adds one test instance and the caller-tooling preservation assertion adds one test. Earlier focused results remain **6/6**, followed by **5/5 in 3.37s / 3.67s** after symlink hardening. Manual harness API+safety acceptance passed in **24.66s** and browser+safety acceptance passed in **32.73s**. Human walkthrough execution remains pending as described below. |
| C267-3 | Integration API info/version/capability plus dashboard/run/content DTOs and schemas -> installed CLI/HTTP/components; current docs/nav -> operator; manual walkthrough and Hub handoff -> future planning only. `validate_phase26_build_inputs` is also the real package-build admission hook. The acceptance CLI explicitly selects its wheel-runtime Python and discovers caller-provided Node/Playwright/library locations. | The installed API assertions require API `1.9`, `dashboard=true`, provider-aware counts/roles, active output, content ownership and unchanged authority hashes. `test_c267_3_build_guard_rejects_stale_frontend_sources` changes a copied production input and requires package validation failure. `test_c267_3_environment_preserves_caller_tooling` checks caller settings. `test_c267_3_docs_and_handoff_match_public_contract` ties docs/handoff to API 1.9, schema 16, the five schemas, explicit Python/tool discovery, manual-pending cleanup and the future-work disclaimer. Static/package/docs gates and the once-only repository suite provide independent integration evidence. | Corrected focused snapshot: **9/9 passed in 98.15s / 98.26s elapsed** with alternate caller-owned tooling paths and Python **3.11.15 + 3.13.14**. Corrected packaging-only inspection, including both Playwright configs in the sdist: **exit 0, 11.52s elapsed**, harness elapsed **10.61s**. Original package gate: **sdist + wheel passed in 29.83s, exit 0**. The single repository-wide run and its corrected exact selector remain as recorded below; the full suite was not rerun. |

## Packaging and installed acceptance

The wheel contains `index.html`, fixed same-origin CSS/JS, the build manifest and
all five public dashboard schemas. The sdist contains production frontend
sources, `package-lock.json`, package/build configuration and repository
acceptance sources, while excluding the historical `tests/fixtures` captures.
Neither distribution contains `node_modules`, Playwright results, runtime
ledger/artifact state or source maps. Each asset is nonempty, <=10 MiB and
digest/size matched to the generated manifest. Missing or stale production
inputs fail package creation.

The final generated assets were HTML **0.45 kB**, CSS **8.58 kB** and JS
**893.57 kB** from **655 modules**. Vite's existing >500 kB advisory was
non-fatal. The installed acceptance resolved `ai_dev_loop` inside its isolated
wheel venv under Python 3.11, not the repository, and served with no Node/npm on
the runtime PATH. Capacity used a fake Codex provider. All browser requests were
loopback; no CDN or external request occurred.

## Exact final validation

Commands used the accepted Node 24.22.0, owned npm/uv caches, owned Playwright
browser libraries and native `/tmp` resources.

| Command | Exact result |
| --- | --- |
| `npm ci` | **exit 0, 156 packages, 0 vulnerabilities, 8.88s elapsed**; npm reported its install-script policy advisory for `esbuild` |
| `npm run typecheck` | **exit 0, 5.71s elapsed** |
| `npm test -- --run` | **exit 0, 12 files / 54 tests passed, 4.50s Vitest / 5.25s elapsed** |
| `npm run build` | Initial final sweep: **exit 0, 655 modules, 4.49s Vite / 5.20s elapsed**. After adding the manifest generator to its own input inventory: **exit 0, 655 modules, 5.34s Vite / 5.99s elapsed**; manifest regenerated. |
| existing `overview`, `linked-inspection`, `attempt-content`, `refresh-lifecycle` Playwright files | **exit 0, 11 passed in 30.7s / 31.98s elapsed** |
| `pytest -q tests/integration/test_phase26_7_installed_dashboard.py` | **exit 0, 6 passed in 64.37s / 65.31s elapsed** |
| `uv run python -m build` | Initial package gate: **exit 0, sdist and wheel built from sdist, 13.26s elapsed**. Final package gate after manifest self-provenance: **exit 0, sdist and wheel built from sdist, 29.83s elapsed**. |
| `uv run python -m ruff format --check .` | Initial gate: **exit 1, 8 accepted Phase 26.1/26.2 files required mechanical formatting, 0.15s**. After formatting exactly those files: **exit 0, 384 files already formatted, 0.11s**. |
| `uv run python -m ruff check .` | **exit 0, all checks passed, 0.13s elapsed** |
| `uv run python -m mypy src` | **exit 0, no issues in 202 source files, 0.88s elapsed** |
| `uv run mkdocs build --strict --site-dir /tmp/ai-dev-loop-phase267-docs-final-1016` | **exit 0, built in 0.86s / 1.40s elapsed**; only the existing Material notice and unlisted incident-page info appeared |
| `git diff --check` | **exit 0, no output** |
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q` | Single authorized repository gate: **exit 1, 1,802 passed / 1 failed / 4 skipped / 29 warnings in 2,912.67s (48:32)**. Only failure: `test_genuine_accepted_v1_frozen_fixture_preserves_hashes_and_materializes`, because Git-restored fixture files were 0644 and the real protected reader requires no group/other bits. |
| exact failed Phase 27.3 selector after test-fixture mode correction | **exit 0, 1 passed in 0.64s / 1.02s elapsed**. No second repository-wide run was executed. |

## Codex correction findings F-01 through F-04

All four reviewer findings were confirmed and fixed. No finding is disputed or
blocked, and no mandatory correction remains unfinished.

| Finding and disposition | Production entry / caller | Independent assertion | Exact correction evidence |
| --- | --- | --- | --- |
| **F-01 — fixed** | Ordinary discovery uses `dashboard-web/playwright.config.ts`, which excludes the installed-only spec. `verify-phase26-dashboard.py::_assert_browser` deliberately uses `playwright.installed.config.ts`; the installed spec retains import-time validation of its owned base URL and fixture for that explicit path. | Playwright ordinary `--list` without either harness variable; dedicated installed `--list` without variables; both installed journeys in the Python matrix. | Ordinary `npm run e2e -- --list`: **exit 0, 11 tests in 4 files, 1.08s elapsed**, with no installed test. Dedicated installed discovery without variables: **expected exit 1 in about 1.1s** with `Installed dashboard acceptance requires its owned base URL and fixture.` Correctly configured installed journeys then passed under both runtimes within the **9/9 in 98.15s / 98.26s** focused result. |
| **F-02 — fixed** | `verify-phase26-dashboard.py --python PATH` -> `_python_identity` -> `uv venv --python PATH` -> isolated `-I` version/import-origin check. `accept` and `prepare` use the explicitly selected interpreter; the supported floor is Python 3.11, not an equality assertion against `sys.executable`. | The installed acceptance test is parameterized over exact Python 3.11 and a supported newer runtime, verifies the reported patch version, and exercises packaging/API/browser/safety from the isolated wheel. | Explicit **Python 3.11.15** and **Python 3.13.14** journeys both passed as part of **9 passed in 98.15s / 98.26s elapsed, exit 0**. A final Python 3.13.14 packaging-only invocation also passed in **11.52s elapsed** and reported `python=3.13.14`. |
| **F-03 — fixed** | `_node_bin` honors `PHASE26_NODE_BIN` or discovers colocated `node`/`npm`/`npx` on `PATH`; `_assert_browser` inherits `PLAYWRIGHT_BROWSERS_PATH` and `LD_LIBRARY_PATH`; the integration test environment preserves all caller settings. No production/test fallback names a historical Phase 26.3 `/tmp` path. | `test_c267_3_environment_preserves_caller_tooling` asserts propagation. The entire corrected file was executed with Node, browser and library paths exposed through `/tmp/ai-dev-loop-phase267-alt-tools-correction/...`, rather than the historical paths. A source search for `phase263` across the harness/test returned no matches. | Alternate-path focused execution: **9 passed in 98.15s / 98.26s elapsed, exit 0**. The final packaging-only invocation used those same alternate paths and passed in **11.52s elapsed**. |
| **F-04 — fixed** | `phase26_installed_fixture.py` independently declares the expected metric units, exact UTC buckets/totals and display strings. `_assert_api` compares the installed API's complete values; `installed-acceptance.spec.ts` compares every rendered date/value and total. The fixture freezes sequence start time before production insertion. | Exact expectations are submissions **0/27/0, total 27**; review rounds **0/3/0, total 3**; process seconds **0/14,400/0, total 14,400**, rendered **0/4/0 hours**. Coverage must report exactly one missing active-attempt duration. These constants are not derived from the dashboard query under test. | An independent fixture/API comparison passed in **14.23s, exit 0** with those exact values. Both installed browser journeys asserted the exact displayed buckets/totals within the corrected **9/9** result. The equality check rejects inflated totals such as inclusion of the excluded chat-creation intervals. |

Correction validation was focused; the repository-wide gate was not repeated:

| Correction command/check | Exact result |
| --- | --- |
| ordinary `npm run e2e -- --list` without installed harness variables | **exit 0, 11 tests in 4 files, 1.08s elapsed** |
| dedicated installed Playwright discovery without harness variables | **expected exit 1 in about 1.1s**, with the explicit owned base-URL/fixture error |
| `npm run typecheck` after the Playwright changes | **exit 0, 3.27s elapsed** |
| independent fixture/API exact activity comparison | **exit 0, 14.23s elapsed** |
| corrected Phase 26.7 integration file with alternate tooling paths and the explicit two-Python matrix | **exit 0, 9 passed in 98.15s / 98.26s elapsed** |
| packaging-only harness under Python 3.13.14 with alternate tooling paths | **exit 0, harness 10.61s / 11.52s elapsed** |
| final docs/tooling assertions | **exit 0, 2 passed in 0.07s / 0.36s elapsed** |
| focused Ruff format/check | **exit 0; 3 files already formatted, all checks passed, 0.24s and 0.18s elapsed**; final changed-test repeat also passed in **0.19s** |
| `mkdocs build --strict` to an owned temporary site | **exit 0, MkDocs 0.78s / 1.31s elapsed**; only the existing Material notice and unlisted incident-page info appeared |
| `git diff HEAD --check`, direct new-config whitespace check, protected-path diff and SHA-256 audit | **exit 0, no whitespace errors; all protected skills/rules/root YAML unchanged** |

The first combined correction Ruff attempt was launched from `dashboard-web/`
with repository-relative Python paths and exited **2 immediately** because the
paths did not exist from that directory; no check ran. Rerunning from the
repository root formatted one file and passed Ruff. This implementer attempt is
execution history only and is not a reviewer round or reviewer-content call.
Three later parallel wrappers also exited **2 in 0.04-0.07s** before running
Ruff/MkDocs because they inherited the workspace's read-only default uv cache.
The exact commands passed after selecting separate owned `/tmp` uv caches, as
reported above; the already isolated pytest wrapper in that batch passed.

## Development attempts and corrections

- The first packaging-only harness run passed wheel/sdist inspection in about
  **10.8s** but returned before printing the informational readiness label; the
  early-return behavior was corrected.
- The first API+safety harness attempt failed in test code with `KeyError` by
  reading a nonexistent `run.timeline.page.totalCount`. Production returned its
  documented page DTO. The harness now uses `run.inspect.attemptCount`; the
  corrected API+safety run passed in **24.66s**.
- Browser+safety acceptance passed in **32.73s**. One manual `prepare` experiment
  could not persist its descendant across the command-execution sandbox
  boundary; separate `cleanup` verified and removed that exact dead-process
  resource. The automated prepare/live-read/cleanup test keeps the parent
  process alive and passed.
- The complete focused file first passed **5 tests in 64.90s** before the
  documentation/handoff assertion was added. The final file passed **6/6** as
  reported above. After audit hardening added a third cleanup-conflict case, the
  affected cleanup/build/docs selection passed **5/5 in 3.37s / 3.67s**; the
  docs assertion alone also passed **1/1 in 0.05s** after tying the walkthrough
  to the actual `fixture-project` value.
- One combined post-audit wrapper invoked the two Ruff paths from
  `dashboard-web/` and exited **2 immediately** because those repository-relative
  paths did not exist from that cwd; no check executed. The commands were rerun
  from the repository root: **2 files unchanged and Ruff passed**, followed by
  the affected **5/5** pytest result.
- The one full repository gate intentionally was not repeated. Its sole failure
  exposed that the accepted Phase 27.3 historical fixture test relied on private
  0600/0700 modes that Git cannot encode. The correction changes no fixture
  bytes or production policy: it restores those modes only on the owned copied
  artifact tree before invoking the real verifier. The failed selector then
  passed, and no broader rerun was justified by a remaining risk.

## Documentation governance and preserved surfaces

The documentation governance skill kept operator claims aligned with current
code and excluded sensitive bodies/identifiers from evidence. The create-plan
skill was read as a required boundary but not invoked because execution was
already explicitly authorized. The read-only staged-review skill was preserved
and not invoked as reviewer B.

Final SHA-256 audit:

- `.agents/skills/create-cursor-plan/SKILL.md`:
  `6784d767cb8de6344ec8d431f8312fe71be6dbefd93c467e9bb772529b547a6a`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`:
  `7563c117439618708906ac9c661506a58b8b90a1f3726b5a55be4a26aac73b4c`
- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`:
  `7e090d56f571852d7687c2d33e3725f44977102abeca182e1248bc610ffd71c3`
- `.cursor/rules/ai-dev-loop-dashboard-contracts.mdc`:
  `aa4e3ae23ad92ba31ea13b6d2d2fbc24baeac97d9163dcd975d2ebe7bcfae853`
- `ai_dev_loop.yaml`:
  `33cd5c07ca3eafb640200df788ee7027eee728192d1e1fa9521d5a45dbe9f187`.

No policy, skill, reviewer/correction/result schema, root YAML or timer was
modified.

## Unfinished mandatory work and human boundary

No Phase 26.7 implementation correction or affected automated selector remains
unfinished. The repository-wide gate itself is recorded honestly as failed on
its only permitted full execution; its only failing selector passed after the
test-fixture permission correction, and the full suite was deliberately not
rerun. This is correction evidence, not a rewritten green full-gate result.

The operator's human walkthrough and optional source-account read-only
observation remain **MANUAL_PENDING**. The delivered `prepare` mode creates the
owned usable site and prints its exact URL/resource/registry; `cleanup` remains
separate. The command-execution sandbox does not retain a detached descendant
for handoff, so no fixture process was left running. No live model/workstation
deployment was attempted or claimed.

Green focused checks and the informational READY_FOR_REVIEW label do not
authorize human acceptance, final Git commit, installation, Hub work or
deployment.
