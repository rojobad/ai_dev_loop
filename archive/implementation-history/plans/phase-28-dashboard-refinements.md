# Phase 28: Dashboard refinements

Status: operator-authorized planning and execution, 2026-10-16. Three sequential
ordinary runs; one final integrated operator walkthrough. This package refines
the merged Phase 26 local dashboard, not the separate ai_dev_loop_hub repository.

## Approved sequence

| Run | Plan | Implementer | Reviewer |
| --- | --- | --- | --- |
| 28.1 | [Short names](phase-28-1-short-names.md) | Codex gpt-5.6-sol / xhigh | gpt-6.1-sol / xhigh |
| 28.2 | [Linked inspection](phase-28-2-linked-inspection.md) | Codex gpt-5.6-sol / xhigh | gpt-6.1-sol / high |
| 28.3 | [Filters and presentation](phase-28-3-filters-and-presentation.md) | Codex gpt-5.6-sol / xhigh | gpt-6.1-sol / xhigh |

Each role has an independent authenticated session, 90-minute timeout and seven
review iterations. Manifest: [phase-28-sequence.yaml](phase-28-sequence.yaml).
The first two accepted runs use the native local checkpoint; the last leaves its
patch staged. No push, PR, merge, installed-package update or timer modification
is authorized by this sequence. Use the stable installed scheduler, never an
editable changing source checkout, to drive this self-hosted work.

## Accepted source and evidence

Observed clean main at `67b8edca5f1868e3263239694edc3e708c94f243`, PR #34 merged.
Phase 26 is shipped, SQLite schema 16, Integration API 1.9; Phase 27 dual
implementers/recovery are present. No Phase 24/25 dependency. Recheck accepted
predecessor code once before dependent implementation. Do not mistake this
planning baseline for a requirement to retain that exact HEAD after checkpoints.

The current RunMap renders every companion card and calls scrollIntoView on chart
selection. It requests 25 rows. AttemptTimeline/AttemptModal emphasize opaque IDs;
metadata displays raw UTC strings and seconds. DashboardApp uses manual Apply.
ActivityCalendar renders all daily text values. DashboardChart's wrapper clips
overflow. Sequence display comes from immutable definition.name; no short-name
metadata exists. Dashboard pages and HTTP collection limits are 100; nextOffset
is offset+limit. The envelope cap is 240 KiB. These are verified implementation
facts and the defects/constraints the three runs own.

## Approved product contract

1. Scheduler-owned editable presentation short names, max **35 Unicode code
   points** after trimming. New preparations require human/agent-selected names
   for standalone runs, sequences and every planned leaf. Nonempty strings only;
   null, numbers, booleans, blank or >35 are errors, not coercions or truncation.
   Reject control characters. No uniqueness requirement. Identity remains IDs.
2. Existing sequences get the first 35 code points of their existing name;
   existing runs use an existing authoritative descriptive name if present,
   otherwise their run ID. Existing planned leaves use phase_name. Trim first;
   use ID when a legacy value is blank. The migration is automatic and rerunnable;
   it never overwrites a subsequently edited alias. Manual renaming is supported
   through explicit local metadata commands, not a Dashboard write endpoint.
3. Display metadata is separate from frozen execution inputs and is not authority
   for state, lineage, project or role. Keep historical payload/artifact hashes,
   existing IDs and execution idempotency identities unchanged. A label-only edit
   never creates or restarts work. Replaying an accepted preparation must retain
   any later manual rename. A replacement run has its own editable metadata,
   initially inheriting its authoritative source/planned leaf alias.
4. Run map: no selected run means no run detail card; selecting a cell shows
   exactly one card and that run's timeline. Never render every run or every leaf
   as visible cards. No page jump or scrollIntoView on chart selection. Keep
   accessible keyboard selection through a compact control or navigable chart.
5. Attempts use stable chronological per-run ordinal labels, for example
   `1 - Iteration 1 - Implementer`, `2 - Iteration 1 - Reviewer`, with the identical
   title in chart, accessible selector and modal. Retries have distinct ordinals.
   Use the authoritative existing timeline ordering with its tie-breaker and
   stable page offset; do not renumber on refresh or infer role from IDs. If the
   current API cannot supply a stable ordinal for live paging, add the minimal
   typed producer metadata. Unclassified attempts remain `Unclassified`.
6. Human metadata precedes Run ID/Attempt ID. Format all displayed instants in
   selected IANA zone as `DD/MM/yyyy HH:mm` (24-hour). Day buckets use
   `DD/MM/yyyy`, retaining their already-zoned day. Duration is `02 h 35 m`;
   positive values below a minute show `<1 m`, zero `00 h 00 m`, unknown unavailable.
   Floor resolved seconds to whole minutes; hours may exceed 99. Exact values may
   be shown in tooltip/detail, never change stored timestamps or metric math.
7. Valid project/preset selection applies immediately; valid typed date/zone
   edits debounce 400 ms. Invalid/incomplete draft retains last valid view and
   shows a concise validation message. Remove Apply; retain explicit Refresh and
   existing Pause/visibility/cancellation controls. A scope change clears obsolete
   selections/readers, and late results cannot restore them.
8. Fetch the entire matching run cohort automatically, requesting up to 1000 per
   batch; retain internal pagination, progress and retry. Only dashboard.runs
   gets the 1000 maximum, not unrelated collections/content. Preserve 240 KiB
   envelopes, 10k-run/100k-attempt/5-second limits and two active client reads.
   A smaller requested page may be retried for a typed page-envelope overflow;
   no catch-all retry and no silent partial or malformed continuation contract.
   All matching IDs are eventually loaded when within the bounds; summary totals
   remain independent of loaded pages. Polling must not discard loaded history.
9. Activity keeps the calendar and metric selector; remove the visible full
   daily list, retain an accessible on-demand equivalent. All graph tooltips
   render above clipping panels and stay inside the viewport, in both themes.
10. Each chart/panel header has a keyboard-accessible Information button that
    opens its explanations, metric/filter definitions and observation details.
    Remove permanent instructional paragraphs. Keep meaningful data, compact
    progress/freshness, errors and partial/unknown evidence visible. Information
    controls must have accessible names, aria-expanded and predictable focus.

## Compatibility and rollout

28.1 must explicitly version any new manifest/input representation that would
otherwise invalidate accepted v1/v2 fixtures. The chosen target is **manifest v3**
for new named sequences (same implementer semantics as v2, required sequence and
per-phase short_name); CLI standalone submit gets required --short-name for new
submissions. Preserve reading/replay/materialization/recovery of already frozen
v1/v2 definitions unchanged. Fresh v1/v2 sequence preparation fails with a clear
upgrade instruction before creating new resources; an already accepted exact
preparation replay remains idempotent. No global fallback for new work. New
short_name inputs populate typed scheduler metadata; do not inject mutable labels
into old frozen execution payloads. Version and publish changed schemas/capability
or additive API minor according to current repository policy.

This phase-28 execution manifest deliberately remains **v2**, because the installed
driver does not yet implement v3 or --short-name. It is prepared before 28.1 and
therefore is a historical prepared sequence by the time new code runs. Its
compatibility is an explicit test case, not permission to install changing code
into the live scheduler. Desired future aliases are `F28 · Dashboard refinements`,
`28.1 · Short names`, `28.2 · Linked inspection`, and `28.3 · Filters and display`.
Do not backfill or rename the user's existing records during implementation.

## Governance, checks and final acceptance

Read AGENTS.md and all applicable .cursor/rules; the new Phase 28 rule scopes
approved exceptions to Phase 26's historical UI contract. The planning/review
skills remain agent policy; do not weaken reviewer/result schemas or root YAML.
Codex authors the narrow preparation-name guidance in this package. Any
control-plane guidance change needs explicit final manual review; automatic
acceptance is implementation evidence only, not that review.

Each run adds production-path contract coverage and reports exact commands,
results and unfinished items under archive/implementation-history/findings/.
Use fake providers, isolated temporary SQLite/XDG/repositories and owned ports;
never tick, migrate, rename, reinstall or inspect sensitive content in the live
operator environment as a test side effect. Node 24 is build/test only. Read
scripts/verify-phase26-dashboard.py and existing harness tooling if Node is absent.
Run scoped regression checks per run; one repository-wide pytest run belongs to
the separate final integration/pipeline gate after all three, not each reviewer
round. If not executed, explicitly report it pending; no invented CI guarantee.
28.3 must build/package and smoke the installed artifact in isolation.

Final manual walkthrough: [phase-28-manual-walkthrough.md](phase-28-manual-walkthrough.md).
Do not pause between runs for manual acceptance. Stop on actual missing contracts,
failed mandatory gates or native scheduler blocks; do not repair them by changing
frozen configuration. User performs final manual acceptance after sequence ends.

## OpenQuestions

None.
