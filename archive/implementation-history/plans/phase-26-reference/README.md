# Phase 26 visual reference

Frozen planning reference copied from the operator-approved dashboard prototype
on 2026-10-14. Every run/sequence/capacity value is illustrative synthetic data.
Files are not an implemented dashboard, source fixtures or scheduler evidence.

- [Standalone preview](dashboard-preview.html): existing interactive mock. It
  loads ECharts 6.0.0 from a CDN and uses a visualization sandbox. It is a visual
  reference only; production must bundle dependencies and use the real read API.
- [Original fragment](dashboard-proposal.html): source of the existing mock.
- [Overview](dashboard-preview.png), [range](time-range-control.png),
  [filtered run map](filtered-run-map.png), [attempt modal](attempt-modal.png),
  [activity hours](activity-hours.png): synthetic screenshots for offline reference.
- [Captured hashes](manifest.json): reference-file identity, not acceptance proof.

Authoritative requirements are the Phase 26 overview/shared contract and each
subphase plan. In particular, effect-kind classification, historical coverage,
all retained states, explicit paging and real artifact ownership take precedence
where the illustrative mock simplified them. No live user content appears here.
