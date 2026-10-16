# Phase 26.5 attempt modal and explicit content findings

Status: **READY_FOR_REVIEW** (informational implementation label only; not a
scheduler state, human acceptance, final commit or deployment authorization).

Implementation worktree: `/home/rojobad/Projects/ai_dev_loop`, branch
`codex/phase-26-local-dashboard`, starting HEAD
`289914742fa71430e3c758944a4434ad717e3545`. Initial Git status was clean. The
accepted Phase 26.1-26.4 and Phase 27.1-27.7 production baseline was used:
Integration API 1.9, SQLite schema 16, exact run/attempt timeline selection,
provider-aware process output, reviewer-only review content, historical Cursor
defaults, current role/retry metadata and authoritative `implementer_retry`
lineage. Phase 24 and Phase 25 were not dependencies. No OpenQuestion remained
unresolved.

The implementation adds an accessible exact-attempt metadata modal and one
memory-only content reader. Opening the modal performs no sensitive read.
Deliberate controls load an initial run prompt as context for either implementer,
an exact per-attempt reviewer prompt or reviewer-owned fix prompt, and exact
attempt stdout/stderr. Reviewer reads require `component=codex` plus a review
effect; a Codex implementer never enters reviewer-content. Correction prompts
remain explicitly unavailable because no public per-attempt binding exists.

The shared reader requests at most 65,536 bytes, retains at most 1 MiB of raw
window ownership and 2 MiB of decoded UTF-16 text, preserves incremental UTF-8
state across chunks, carries only an incomplete UTF-8 tail into an explicitly
advanced window, and keeps literal search bounded to the current window. Active
EOF, `hasMore`, completion, `nextOffset`, source truncation and unavailability
remain independent. Integrity/replacement failure retires old decoded content.
Close, reselect, filter/run retirement and unmount abort requests, advance
ownership generations and clear content/search state; a late reply cannot restore
the modal or a prior target.

Correction finding **F-01 is FIXED**. Paging actions now move focus synchronously
to the stable, programmatically focusable reader region before `Load more`,
`Check for new bytes` or `Next bounded window` can be removed. The modal trap
also recovers forward and reverse Tab when the active element is outside the
dialog or is no longer in its tabbable set. The reader region remains bounded,
memory-only and visibly outlined on keyboard focus. Escape behavior and exact-row
focus restoration are unchanged.

No scheduler transition, migration, reservation, recovery, Git action, real
provider turn, timer, root YAML, review/result schema, operator installation or
live ledger/artifact state changed. Automated tests used temporary owned state,
fake providers and a production loopback server. Browser trace, screenshot and
video recording remained disabled. Content bodies were not written to browser
storage or logs.

## Admission and prerequisite evidence

The accepted prerequisite was checked once before dependent implementation.
History/source inspection confirmed accepted commits `161e6eb` (26.1),
`20672c5` (26.2), `6906762` (26.3), `2899147` (26.4), current API 1.9/schema 16,
the typed Phase 26.2 content dispatcher, the exact Phase 26.4 run/attempt
selection, and the Phase 27 provider-aware effect/component contracts. The
worktree was clean. This source checkout was not used as a live scheduler driver.

```bash
UV_CACHE_DIR=/tmp/ai-dev-loop-phase265-uv-cache TMPDIR=/tmp TMP=/tmp TEMP=/tmp \
  uv run python -m pytest -q \
  tests/integration/test_phase26_1_dashboard_queries.py \
  tests/integration/test_phase26_2_dashboard_server.py \
  tests/integration/test_phase27_3_inspection.py
```

Result: **exit 0, 29 passed in 45.07s pytest / 45.56s elapsed**. This admission
selection was not repeated.

## Contract evidence

| ID | Production entry and caller | Independent test/assertion | Exact executed result |
| --- | --- | --- | --- |
| C265-1 | `AttemptTimeline` exact typed selection -> keyed `AttemptModal` metadata -> explicit control -> typed `DashboardDataSource` -> `LocalFetchDashboardDataSource` fixed operations. `AttemptModal` gates review content on the reviewer component/effect pair; implementers use `run.initial-prompt` context and `run.output`, never `run.review-content`. | `AttemptModal.test.tsx` proves zero content calls on open; exact reviewer run/attempt/kind; Codex implementer context ownership; correction unavailability without fabricated bytes; historical `not_recorded` without a content call; and Escape/focus return. `attempt-content.spec.ts` runs the packaged UI against the real loopback server, inspects both implementer and reviewer attempts, and asserts exact operations/IDs. | Mandatory frontend selection: **2 files / 9 tests passed in 1.57s Vitest / 2.04s elapsed, exit 0**. Browser acceptance: **2 passed in 6.5s Playwright / 7.49s elapsed, exit 0**. |
| C265-2 | Existing `IntegrationRunReadService`, `IntegrationReviewReadService` and `IntegrationProcessOutputReadService` -> `DashboardHttpDispatcher` -> explicit parsers/DTOs -> `ContentReader` incremental decoder and bounded window. | `ContentReader.test.tsx` independently splits a UTF-8 code point across chunks, advances through a >1 MiB source, proves 64 KiB requests/1 MiB windows, active EOF then completion, source truncation, literal escaped text and integrity retirement. `test_phase26_5_attempt_content.py` drives fake Cursor/Codex providers through real protected readers and HTTP, compares frozen/review bytes with authoritative artifacts, checks complete+hasMore process output and rejects implementer reviewer-content. Phase 21.4 historical/multichunk selectors and the four required auth/reader unit files cover `not_recorded`, partial/invalid evidence, wrong bindings, cancelled-active semantics, truncation conflicts, replacement/hash/path failures and frozen-artifact EOF. | Focused Python content selection: **27 passed in 36.65s pytest / 37.14s elapsed, exit 0**. Mandatory frontend reader tests are included in the **9/9** result above. |
| C265-3 | In-flight `ContentReader` request -> per-target `AbortController` + generation/request serial -> keyed modal retirement -> late response fence; paging action -> stable reader focus -> robust `AttemptModal` containment; `AttemptTimeline.closeAttempt` restores the exact companion row. | `ContentReader.test.tsx` synchronizes a held old owner, reselects, verifies abort, resolves the late reply and proves no old body/search restoration; integrity failure clears prior content; load-more, active-output refresh and next-window assertions prove focus relocation through terminal controls. `AttemptModal.test.tsx` proves forward/reverse recovery from an outside element plus normal wrap, Escape and exact-row restoration. The production-server browser fixture holds both an initial sensitive response and a second page of synthetic multi-chunk output. The synchronized paging regression proves focus is inside during loading, forward/reverse Tab remain inside before and after terminal release, and Escape restores the originating attempt row. | Correction final results: mandatory frontend selection **2 files / 9 tests passed in 1.84s Vitest / 2.65s elapsed, exit 0**; Phase 26.5 browser acceptance **3 passed in 7.5s Playwright / 8.42s elapsed, exit 0**; affected Phase 26.3/26.4 browser regressions **3 passed in 10.3s / 11.20s elapsed, exit 0**. |

## F-01 correction evidence

The confirmed defect was in `ContentReader` and `AttemptModal`: paging changed
reader state to loading and unmounted the activated action, while the document
Tab handler only wrapped an element already equal to the first or last dialog
control. Chromium could therefore place focus on `BODY`, and the following Tab
could reach a dashboard control behind the modal.

The correction adds no new content read or automatic refresh. `ContentReader`
focuses its stable `section[tabindex=-1]` before starting each existing explicit
paging path. `AttemptModal` treats an active element outside the dialog or absent
from the current tabbable set as a containment edge and sends forward Tab to the
first control or reverse Tab to the last. Existing `AbortController`, generation,
request serial, one-target ownership, raw/decoded bounds and Escape cleanup are
unchanged.

The browser regression uses only temporary owned control files and synthetic
output. Its first 65,536-byte page is returned normally; the next response is
held after the paging request reaches the fake service and then explicitly
released as a terminal chunk. It asserts focus containment before any Tab,
forward/reverse Tab containment while the page is held, the same checks after
terminal completion, and Escape restoration to the exact row. Browser traces,
screenshots and videos remain disabled.

| Correction validation command | Exact result |
| --- | --- |
| `npm run typecheck` | **exit 0, 3.87s elapsed** |
| `npm test -- --run src/dashboard/AttemptModal.test.tsx src/dashboard/ContentReader.test.tsx` | **exit 0, 2 files / 9 tests passed, 1.84s Vitest / 2.65s elapsed** |
| `npm run build` | **exit 0, 652 modules, 5.56s Vite / 6.25s elapsed**; packaged HTML 0.45 kB, CSS 8.16 kB and JS 824.24 kB; the existing >500 kB advisory remains non-fatal |
| `npx playwright test e2e/attempt-content.spec.ts` | **exit 0, 3 passed in 7.5s / 8.42s elapsed** |
| `npx playwright test e2e/linked-inspection.spec.ts e2e/overview.spec.ts` | **exit 0, 3 passed in 10.3s / 11.20s elapsed** |
| focused Ruff format/check on `fixture_server.py` | **exit 0; 1 file already formatted in 0.07s; all checks passed in 0.10s** |
| `git diff HEAD --check` | **exit 0, 0.01s elapsed** |

Correction development attempts are not hidden. A build invocation from the
repository root exited **254** because that directory has no `package.json`; the
same build command was rerun from `dashboard-web` and passed. The first full
browser attempt reported **2 passed / 1 failed in 37.5s**, and a 60-second
isolated diagnostic also timed out: the unscoped `Load more` locator immediately
selected the already-present background `Load more runs` control, which the
backdrop correctly prevented from receiving pointer events. A bounded 5-second
diagnostic reproduced that exact locator error. Scoping the exact `Load more`
name to the modal fixed the test harness; the isolated regression then passed
**1/1 in 5.2s / 6.13s elapsed**, followed by the final **3/3** browser result.
The first timed validation wrapper also exited **127 in 0.00s** because its
process did not receive the owned Node path; the identical final commands above
were rerun with the owned Node 24.22.0 path and passed.

## Implemented boundaries

- Public frontend types and parsers model frozen prompt, review detail/content and
  process output separately. Ownership IDs, content kind, component, stream,
  offsets, lengths, completion and continuation invariants are checked before
  bytes enter the reader.
- Initial prompt context remains run-owned and is clearly labelled as not the
  exact execution envelope. Exact correction prompts are not reconstructed from
  review order, run IDs, private paths or provider output.
- Reviewer availability detail is itself loaded only after a deliberate review
  content action. Historical unavailable reasons remain visible; no empty or
  guessed content substitutes for them.
- React text rendering is used for source bytes; no HTML or Markdown execution is
  present. The only search is a literal in-memory search over the current bounded
  decoded window.
- Process output can expose complete data with later chunks, active EOF with an
  explicit same offset, and source truncation independently. New active bytes are
  checked only by an explicit button; no sensitive reader polls.
- A modal owns only one target. Selecting another target unmounts the old reader;
  selecting another attempt remounts the modal by exact run/attempt key. Close and
  route/filter/run retirement unmount the reader and clear its refs/state.
- Existing SQLite/artifact writers, scheduler state, lineage/metrics, capacity,
  provider/retry semantics and server admission limits were not changed.

## Exact focused validation

Commands used the previously accepted owned Node 24.22.0, Playwright browser and
browser-library locations, plus an owned uv cache and temporary XDG/resources.

| Command | Result |
| --- | --- |
| `npm run typecheck` | **exit 0, 2.88s elapsed** |
| `npm test -- --run src/dashboard/AttemptModal.test.tsx src/dashboard/ContentReader.test.tsx` | **exit 0, 2 files / 9 tests passed, 1.57s Vitest / 2.04s elapsed** |
| affected `AttemptTimeline`, local-fetch, parser and `DashboardApp` Vitest selection | **exit 0, 4 files / 20 tests passed, 2.14s Vitest / 2.66s elapsed** |
| `npm run build` | **exit 0, 652 modules, 4.43s Vite / 4.84s elapsed**; packaged HTML 0.45 kB, CSS 8.13 kB, JS 824.00 kB; the existing >500 kB advisory remains non-fatal |
| `npx playwright test e2e/attempt-content.spec.ts` | **exit 0, 2 passed in 6.5s / 7.49s elapsed** |
| affected `linked-inspection.spec.ts` + `overview.spec.ts` | **exit 0, 3 passed in 10.3s / 11.23s elapsed** |
| focused Python selection listed below | **exit 0, 27 passed in 36.65s / 37.14s elapsed** |
| focused Ruff format/check on the changed Python fixture/test | **exit 0; 2 files already formatted; all checks passed** |
| `uv run mypy src` | **exit 0, 202 source files, 0.62s elapsed** |
| `uv run mkdocs build --strict --site-dir <owned-temp>` | **exit 0, built in 0.70s / 1.01s elapsed**; only the existing informational Material notice and unlisted incident pages were reported |
| `git diff --check` | **exit 0** |

Focused Python command:

```bash
TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q \
  tests/integration/test_phase26_5_attempt_content.py \
  tests/integration/test_phase26_2_dashboard_server.py::test_c262_2_real_provider_owned_output_and_reviewer_content_boundaries \
  tests/integration/test_phase21_4_acceptance.py::test_c04_full_content_kinds_multichunk_and_fix_prompt_isolation \
  tests/integration/test_phase21_4_acceptance.py::test_c05_historical_absence_partial_and_invalid_json_bytes \
  tests/unit/integration_api/test_process_output_auth.py \
  tests/unit/integration_api/test_process_output_resolution.py \
  tests/unit/integration_api/test_review_evidence_auth.py \
  tests/unit/integration_api/test_artifact_reader.py
```

Development attempts are not hidden. The first direct `npm run typecheck` exited
**127** before type checking because this non-interactive shell did not include
the owned Node directory on `PATH`; the documented owned Node path was then used.
The first two-file Vitest attempt had **1 failed / 7 passed** because one test
asked for a unique `yes` even though completion and source truncation were both
independently `yes`; the assertion was corrected to preserve both fields. The
affected tests were rerun and the final mandatory selection passed **9/9**.

## Preserved surfaces and unfinished work

The planning/documentation/reviewer skills, dashboard governance rule and root
YAML are unchanged. Final SHA-256 audit:

- `.agents/skills/create-cursor-plan/SKILL.md`:
  `6784d767cb8de6344ec8d431f8312fe71be6dbefd93c467e9bb772529b547a6a`
- `.agents/skills/ai-dev-loop-docs-acceptance-governance/SKILL.md`:
  `7563c117439618708906ac9c661506a58b8b90a1f3726b5a55be4a26aac73b4c`
- `.agents/skills/review-staged-ai-dev-loop-execution/SKILL.md`:
  `7e090d56f571852d7687c2d33e3725f44977102abeca182e1248bc610ffd71c3`
- `.cursor/rules/ai-dev-loop-dashboard-contracts.mdc`:
  `aa4e3ae23ad92ba31ea13b6d2d2fbc24baeac97d9163dcd975d2ebe7bcfae853`
- `ai_dev_loop.yaml`:
  `33cd5c07ca3eafb640200df788ee7027eee728192d1e1fa9521d5a45dbe9f187`

The separate read-only review skill was read as an implementation boundary but
was neither invoked as reviewer B nor modified. The applicable documentation
governance skill kept this evidence grounded in code/tests and excluded content
bodies from the handoff.

No Phase 26.5 contract or mandatory focused check is unfinished. The
repository-wide pytest gate, isolated wheel/sdist installation acceptance, live
operator-source observation, final operator walkthrough and human visual
acceptance were deliberately **not executed**; the approved plan assigns those
combined gates once to Phase 26.7. Green checks do not authorize human
acceptance, a final commit or deployment.
