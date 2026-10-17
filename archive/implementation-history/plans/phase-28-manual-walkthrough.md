# Phase 28 final manual walkthrough

Status: automated implementation gates complete; operator acceptance pending.
Perform after all three
runs complete, from a separately updated installed build or an owned isolated
fixture installation. This document does not authorize updating the live package,
timer, real data or final Git publication. 28.3 must replace planned command
examples below with verified actual help/output where necessary.

Use the installed command surface below; do not run it from an editable checkout
or against the live operator ledger for fixture acceptance:

```bash
ai_dev_loop integration info
ai_dev_loop dashboard serve --port 8765
```

1. Review the implemented preparation guidance in AGENTS.md/planning skill,
   --short-name, manifest v3 and the two metadata-only set-short-name commands.
   In an isolated fixture create a named standalone run and a named sequence
   with names per phase. Verify missing/>35/blank names rejected, metadata rename
   preserves ID, state and frozen inputs. No real model call is needed.

   ```bash
   ai_dev_loop scheduler sequence prepare --repo-path /path/to/repo --manifest /path/to/manifest-v3.yaml
   ai_dev_loop scheduler run set-short-name <run-id> --short-name "Renamed run"
   ai_dev_loop scheduler sequence set-short-name <sequence-id> --short-name "Renamed sequence"
   ```
2. Use an upgraded historical fixture; existing runs/sequences/planned leaves
   show truncated names without manual filling. Rename a fixture sequence and
   run through the explicit CLI and Refresh; short labels change without
   restarting or recreating execution. Pre-existing v2 materialization still works.
3. Start owned fixture Dashboard and choose project/range/zone. No Apply button:
   project/preset commits immediately, typed edits commit after 400 ms once valid.
   Invalid range preserves last view with explanation. Test America/La_Paz and
   another IANA zone, verifying DD/MM/yyyy HH:mm and exclusive upper bound.
4. Map initially shows no run card. Click a run: exactly one card and its timeline;
   click another: they change without jumping page position. Keyboard can select
   the same IDs. A long sequence never creates a wall of cards.
5. Timeline attempts and modal share titles such as 1 - Iteration 1 - Implementer;
   retries receive different stable ordinals. Dates/durations are human formatted
   and IDs last. Sensitive prompt/stdout/stderr remain unloaded until clicked;
   switching run/filter or closing retires the old reader. Logs stay raw.
6. Use >1000 matching fixture runs. Map automatically loads all, shows progress,
   requires no Load more, and handles page overflow in smaller batches. Refresh
   and polling preserve coherent selection/history. Fail a later page: incomplete
   coverage/error is visible and retry works; do not mistake partial data for totals.
7. Information buttons expose each panel's explanation via mouse and keyboard;
   closing restores focus. Warnings remain visible. Activity's large daily list
   is gone; accessible daily values remain available on demand. Switch all three
   metrics and verify time is formatted as hours/minutes without changing math.
8. At 1095x884 and a narrower viewport, in light and dark themes, hover the first
   calendar rows and
   other charts. Tooltip stays visible above panels and inside viewport. Test
   Pause/Resume, hidden-tab behavior and explicit Refresh. No sensitive polling.
9. Record observed results and any defects. Automatic sequence completion is
   not manual acceptance. The pipeline's single full-suite gate is reported
   independently from this walkthrough and focused tests.
