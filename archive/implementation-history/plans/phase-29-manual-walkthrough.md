# Phase 29 dashboard token widgets manual walkthrough

Status: **MANUAL_PENDING**. This checklist is the joint human pass for the
token query and the Dashboard widgets. Writing it, or passing the automated
checks, is not human acceptance, a commit, or permission to enable the timer,
install integrations, or control a live scheduler.

## Prepare an isolated site

Use a current source checkout after the Phase 29.2 focused validation. Serve
the built dashboard from that checkout without touching the operator's
scheduler state:

```bash
ai_dev_loop dashboard serve --port 8765
```

Open exactly `http://127.0.0.1:8765/`. The process binds loopback only. Do not
enable the systemd timer, run `integrations install`, abort or start a live
run, or point the server at a scheduler ledger you are not prepared to read.

If the installed producer predates `dashboardTokenUsage`, the token panels say
the usage is not available. The overview, map, timeline, and activity remain
usable. That absence is not a zero.

## Walkthrough

- Apply an unfiltered range and then one project. Confirm the scope applies on
  its own after the short typing pause, and that Refresh current scope reloads
  the same range.
- On the token panels, click a configured model and a role in the flow or
  composition chart. Confirm the same choice appears in the Model and Role
  selects, that Clear selection restores the full cohort, and that the three
  charts agree on the known total.
- Read one heatmap row without adding its columns together. The columns
  overlap. An em dash is unknown, a printed zero is a measured zero, and a
  partial cell is a lower bound.
- Select one exact run from the map. Its consumption shows that run's short
  name and direct total. Changing the global model or role does not change it.
  Selecting another run replaces it. Clearing the run removes the direct read.
- Find a partial or unknown cohort and a recorded zero. Confirm none of them
  is drawn as a guessed positive amount, and that an unsupported producer does
  not blank the rest of the page.
- Leave information icons and Precise values closed until you open them. The
  opened information returns focus on Escape without moving the page. Precise
  values put the human description before the run ID.
- Hover a chart. The tooltip stays inside the window, shows the exact amount
  in readable words, and does not include a run or attempt ID. A model name
  that contains markup is shown as text.
- Change the selected IANA zone and confirm dates follow that zone. Durations
  stay in hours and minutes.
- Check a wide window, where composition and categories sit side by side, and
  a narrow window, where they stack. The category chart scrolls inside its own
  box. The run map keeps its own scroll.
- Scroll to the bottom, then Pause and Refresh while a read is slow. The page
  position and the token section stay put. Resume automatic refresh afterward.
- Confirm the initial view and the token polling do not open a prompt, a
  review, or process output until you use those inspection actions yourself.

Stop at any disagreement between the chart, the selected run, and the scope.
Do not treat this document as an acceptance record.
