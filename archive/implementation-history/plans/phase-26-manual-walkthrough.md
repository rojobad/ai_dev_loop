# Phase 26 dashboard manual walkthrough

Status: **MANUAL_PENDING**. This checklist prepares an owned fake-data site for
human inspection. Completing automated checks or this document is not human
acceptance, a commit or deployment authorization.

## Prepare the installed fixture

Run from the Phase 26 source checkout after its focused validation:

```bash
uv run python scripts/verify-phase26-dashboard.py prepare \
  --python /ruta/a/python3.11 \
  --registry /tmp/ai-dev-loop-phase26-dashboard-$(id -u).json
```

The command builds the frontend and distributions, installs the wheel into its
own private venv, creates an isolated XDG home plus fake Cursor/Codex providers,
starts the installed CLI outside the checkout, and prints one record like:

```text
MANUAL_PENDING url=http://127.0.0.1:<port>/ resource=/tmp/ai-dev-loop-phase26-dashboard-<random> registry=/tmp/ai-dev-loop-phase26-dashboard-<uid>.json
```

Use exactly that printed URL, resource root and registry. The private resource
contains `.owner.json`, the isolated installed venv/runtime, `fixture.json`,
`engine.sqlite3` and protected fake artifacts. It does not use or control the
operator's scheduler, providers, timer, repository reservation or source ledger.
The fixture project is `fixture-project`; apply UTC range
`2026-10-14T00:00` through exclusive `2026-10-17T00:00`.

`--python` selects the explicit supported runtime (`>=3.11`) used for the
isolated installation. Run automated `accept` separately with another supported
runtime when validating a runtime matrix; one prepared site is sufficient for
this human walkthrough.

If preparation reports `BLOCKED_CONTRACT`, stop. Preserve its exact error and
any recovery marker; do not delete a resource whose identity is uncertain.

## Walkthrough

- Confirm the URL is `http://127.0.0.1:<port>/`, no remote resources load, and
  the page identifies itself as the local read-only dashboard.
- Apply project `fixture-project`, the UTC range above, and zone `UTC`.
  Confirm KPIs report 27 started runs and the overview shows project/state,
  workload and current fake Codex capacity with dated observations/coverage.
- In Activity, inspect `submissions` (27), `review-rounds` (3), then
  `process-hours` (4 hours, with daily values 0/4/0). Confirm the UI explains
  event-date versus started-cohort semantics and converts the exact 14,400
  backend process seconds to hours only for display.
- In Sequence and run map, load the second page and select the replacement run.
  Confirm provider `codex`, generation/lineage `implementer_retry`, source run
  binding and exact project are visible; no relationship is inferred from IDs.
- In Attempt timeline, load the second page. Confirm 26 recorded attempts and
  distinct labels for Cursor implementation, Codex implementation and Codex
  review. Active/incomplete attempts must not show an invented end/duration.
- Open the selected Codex implementer attempt. Confirm opening the modal alone
  loads no prompt/output. Deliberately load initial run context, stdout and
  stderr. Confirm there is no per-attempt implementer-prompt action and no
  reviewer-content request for this attempt.
- Open the historical reviewer attempt and request the exact review prompt; it
  must show `not_recorded`. Open the later reviewer attempt and confirm its exact
  recorded prompt loads. This is evidence availability, not reconstruction.
- Select the source run, open its active attempt, load stdout and confirm the
  process is labelled active/incomplete with `Check for new bytes` available.
- Press `Pause automatic refresh`, then `Refresh applied scope`. Confirm the
  explicit read occurs while automatic refresh remains paused and sensitive
  content does not load. Hide/show the tab and Resume; no catch-up burst should
  occur. A compatible last success may remain visible with its original date if
  a read fails.
- Switch theme and reload once. Confirm only the theme persists: filters,
  selection, modal and content are not restored from browser storage.
- Stop/restart only this fixture if testing recovery. The new browser session
  must have no durable query/content cache and the ledger/artifact hashes must
  remain unchanged.

An optional read-only observation against the operator's installed source
account is separate: start `ai_dev_loop dashboard serve` explicitly and only
inspect. Do not run model turns, scheduler ticks/retries/abort, installation,
timer operations, ledger repair or Git actions as part of this walkthrough. Do
not copy sensitive prompt/output text into findings.

Record the date, browser, printed URL/registry identifiers (not content), each
item's pass/fail and any discrepancy. Human acceptance remains pending until the
operator records that decision independently.

## Exact cleanup

Cleanup is deliberately separate:

```bash
uv run python scripts/verify-phase26-dashboard.py cleanup \
  --registry /tmp/ai-dev-loop-phase26-dashboard-$(id -u).json
```

Expected receipt:

```text
CLEANED resource=/tmp/ai-dev-loop-phase26-dashboard-<random> recoverable=false
```

Cleanup validates uid, owner token, exact resource root, PID start time,
executable argv and process group before termination/removal. A conflict or
uncertain termination returns `BLOCKED_CONTRACT` and preserves the resource plus
registry for manual recovery; never broaden deletion to another `/tmp` path.
