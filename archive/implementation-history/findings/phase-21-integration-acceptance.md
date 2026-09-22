# Phase 21 — Local Integration API Acceptance

Status: Phase 21.6 correction turn (F-01–F-06). Live provider capacity and Bridge/Web
UI deployment remain **unexecuted** unless separately authorized. **C-06** repository
regression, package build, and strict MkDocs build **executed this turn** (see validation
table). Phase 21.6 automated acceptance remains **partial** because **F-04** historical
API **1.2–1.4** fixtures are **blocked** on external inputs; **C-05** is **incomplete**
for those minors.

## Correction findings (21.6)

| ID | Status | Summary |
|---|---|---|
| F-01 | **Fixed** | Unrepresentable `resetsAt` → `null` in probe + projection; tests in `test_capacity_projection.py` and `test_phase21_6_capacity.py`. |
| F-02 | **Fixed** | Valid `att-{32 hex}` attempt IDs in acceptance setup. |
| F-03 | **Fixed (closed by review)** | C-04 Bridge acceptance preserved from prior correction: `bridge_acceptance_constants.py` + `bridge_supervision_traversal.py` (prompt/response branches, latest Cursor oracles, independent sequence report expectations, public CLI traversal, output/pagination/historical read). **No code change this turn.** |
| F-04 | **Blocked (external prerequisite)** | Not an implementation defect. Missing accepted producer JSON for Integration API minors **1.2, 1.3, 1.4** with provenance and pinning authorization. Consumer gate: `src/ai_dev_loop/integration_api/consumer.py` (`evaluate_envelope_major`, `ConsumerGate`). Dependent **C-05** historical acceptance for 1.2–1.4 **not implemented**; in-repo fixtures stop at `tests/fixtures/integration_api/v1_0/`, minimal `v1_1/`, and `v1_5/` only. |
| F-05 | **Fixed** | Timeout/frozen-codex/unavailable CLI tests; `FAKE_CODEX_CAPACITY_PROBE_TOUCH_FILE` in fake codex. |
| F-06 | **Fixed** | Restored six-subphase contract inventory below. |

### F-03 — why the prior correction missed the contract

The first correction turn wired traversal helpers but did not **enable** strict review or
process-output requirements on sequence runs in
`test_c04_full_supervision_traversal_public_cli_only` (`process_output_run_ids=frozenset()`,
`strict_review_content_run_ids` standalone-only). Checks used prefix/substring acceptance,
`_fetch_all_chunks` double-counted pagination, and unavailable required streams were
skipped with `continue`. Live consumption used scheduler-private attempt lookup and did
not assert stderr or independent final bytes.

The **second** correction turn closed output/pagination/live gaps and required prompt bytes
on all review branches, but `verify_captured_review_prompt_bytes` still treated the latest
Cursor section as optional content: only section markers and the embedded **initial**
prompt were checked, so an arbitrary string could replace the latest final response. Sequence
reports were gated by SHA256 plus shallow JSON checks (`schema_version`, `sequence_name`,
minimum phase count, allowed `final_outcome`) that accepted empty phase objects. Report
semantics were not independently specified apart from hashing the generated artifact.

The **third** correction turn (this turn) closes those gaps without weakening output,
pagination, discovery, or historical Bridge coverage.

### F-04 — external evidence required (blocked)

Stop API **1.2–1.4** historical compatibility acceptance until all of the following exist:

1. **Accepted producer artifacts** — full public JSON envelopes (info, run list/inspect,
   sequence list/inspect, review detail, capacity where applicable) captured from each
   accepted Integration API minor **1.2, 1.3, and 1.4** release, with checksums.
2. **Provenance** — source tag/commit or release identifier, capture date, and capture
   command/environment (fake CLI versions, schema versions).
3. **Authorization to pin** — explicit product/operator decision to add those captures under
   `tests/fixtures/integration_api/` (or documented external pin path) as read-only
   baselines; not regenerated from the current 1.5 serializer.

In-repo partial evidence only: `v1_0/`, minimal `v1_1/run_list_data_minimal.json`,
`v1_5/codex_capacity_available_primary_only.json`, and provenance-backed
`tests/fixtures/phase21_4_pre_21_4_historical/` (pre-21.4 review prompt absence).

No further in-repository reconstruction of 1.2–1.4 envelopes is attempted without (1)–(3).

**Why prior corrections could not close F-04:** The repository never received trustworthy
captures or operator authorization to pin them. Partial v1_0/v1_1/v1_5 material and
schema-shaped stubs do not substitute for producer envelopes at minors **1.2–1.4**.
Regenerating fixtures from the current 1.5 serializer would invent history and is out of
scope.

**Decision needed to unblock:** Supply (1)–(3) above; then verify provenance and add the
planned pinned consumer / capability-gating tests for **1.2, 1.3, and 1.4** before claiming
F-04 or full **C-05** closure.

### F-05 — C-05 compatibility (in scope, not F-04)

| Test | What it proves / does not prove |
|---|---|
| `test_c05_pinned_v1_0_consumer_tolerates_additive_fields_on_fixture` | Pinned required-field logic on **mutated file**; does not prove live 1.5 `integration info`. |
| `test_c05_pinned_v1_0_consumer_accepts_live_api_info` | **Live** 1.5 info through pinned v1.0 required-field consumer + major gate ACCEPT. |
| `test_c05_v1_1_minimal_run_list_historical_shape` | Schema shape only; not a captured 1.1 public envelope baseline. |
| `test_c05_major_mismatch_blocks_resource_requests_on_fixture` | Consumer helper on fixture; not Bridge subprocess path. |
| `test_c05_bridge_client_stops_after_info_major_mismatch_envelope` | **Normal** client path: `evaluate_envelope_major` on mismatched info subprocess stdout; **zero** resource requests before `UPDATE_REQUIRED` block. |

**C-05 gap (F-04):** API **1.2**, **1.3**, and **1.4** pinned consumer/historical acceptance tests remain **unimplemented** until F-04 artifacts and pinning decision exist.

## Contract inventory (Phases 21.1–21.6)

| Phase | ID | Production entry | Primary tests | Validation (this correction turn) |
|---|---|---|---|---|
| 21.1 | C-01 | `commands/integration.py` → `integration info` | `tests/unit/integration_api/test_contract.py`, `tests/integration/test_phase21_1_integration_contract.py` | **Prior bundle: executed**; not rerun this turn |
| 21.1 | C-02 | JSON envelope / Typer adapter | same | **Prior bundle: executed** |
| 21.1 | C-03 | `integration_api/consumer.py` major gate | `test_contract.py` | **Prior bundle: executed** |
| 21.1 | C-04 | Read-only XDG boundary | `test_contract.py` | **Prior bundle: executed** |
| 21.1 | C-05 | Packaged schemas | `test_contract.py` | **Prior bundle: executed** |
| 21.1 | C-06 | Offline wheel smoke | `test_phase21_1_integration_contract.py` | **Prior: skipped/executed** if wheelhouse present |
| 21.2 | C-01–C-06 | Run list/inspect/artifacts | `tests/integration/test_phase21_2_run_inspection.py` | **Prior bundle: executed** |
| 21.3 | C-01–C-06 | Sequence inspection | `tests/integration/test_phase21_3_sequence_inspection.py` | **Prior bundle: executed** |
| 21.4 | C-01–C-06 | Review evidence | `tests/integration/test_phase21_4_review_inspection.py`, `test_phase21_4_acceptance.py` | **Prior bundle: executed** |
| 21.5 | C-01–C-06 | Process output | `tests/integration/test_phase21_5_process_output.py` | **Prior bundle: executed** |
| 21.6 | C-01 | `integration codex-capacity` → `capacity_service` → probe | `test_phase21_6_capacity.py`, `test_capacity_projection.py` | **Executed this turn** (focused bundle) |
| 21.6 | C-02 | Scheduler classifier | `test_phase19_codex_capacity.py`, `test_phase20_5_reliable_codex_limit_detection.py` | **Prior bundle: executed** |
| 21.6 | C-03 | Normalization / unavailable | `test_capacity_projection.py`, `test_phase21_6_capacity.py` | **Executed this turn** (focused bundle) |
| 21.6 | C-04 | Fake Bridge public CLI | `fake_bridge_client.py`, `bridge_supervision_traversal.py`, `bridge_acceptance_constants.py`, `test_phase21_6_integration_acceptance.py`, `test_bridge_supervision_traversal.py` | **Executed this turn** — see validation table |
| 21.6 | C-05 | Cross-minor/major consumers | `integration_api/consumer.py`, `test_phase21_6_integration_acceptance.py` (C-05 tests) | **Partial:** v1_0/v1_1/live 1.5 tests **executed** (prior + full suite); **1.2–1.4 blocked (F-04)** |
| 21.6 | C-06 | Whole product + docs | Full suite, `uv build`, `mkdocs build --strict` | **Executed this turn** — see validation table |

## Validation commands

| Command | Result (this correction turn) |
|---|---|
| Code changes | **None** (F-04 blocked on external inputs; F-03 closed by review) |
| `TMPDIR=/tmp TMP=/tmp TEMP=/tmp uv run python -m pytest -q` (full suite, C-06) | **Executed**, **1317 passed, 3 skipped, 29 warnings in 1737.37s** |
| `uv run python -m build` (C-06) | **Executed**, **success** (`ai_dev_loop-0.1.0` sdist + wheel) |
| `uv run mkdocs build --strict` (C-06) | **Executed**, **success** (site under `site/`, not tracked) |
| Prior focused 21.6 bundle (55 tests) | **Executed prior correction turn**, **55 passed in 428.43s** — not rerun this turn |
| Plan §Testing Criteria Phase 21-only pytest bundle | **Unexecuted this turn** (full suite supersedes for C-06 regression) |
| Offline wheel smoke in isolated env (`test_phase21_1_integration_contract.py`) | **Unexecuted this turn** (not separately rerun; wheel build succeeded) |
| Live provider / Bridge/Web UI deployment | **Unexecuted** (not authorized) |

**Final phase acceptance:** **Not claimed.** C-06 regression passed, but **F-04** remains
**blocked** and **C-05** is **incomplete** for API **1.2–1.4** historical consumers.

## OpenQuestions

- Pinning process for provenance-backed API **1.2–1.4** fixtures (F-04).
