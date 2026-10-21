import React, { useEffect, useMemo, useRef, useState } from "react";

import { DashboardChart } from "./DashboardChart";
import { InformationControl } from "./InformationControl";
import {
  SELECTED_RUN_REFRESH_MS,
  type RefreshCoordinator,
  canonicalReadKey,
} from "./refreshCoordinator";
import { formatInstant } from "./time";
import {
  coverageLabel,
  configuredModelLabel,
  flowLabel,
  formatTokenExact,
  heatmapColumnLabel,
  isUnsupportedTokenUsage,
  nativeCategoryLabel,
  projectTokenUsage,
  roleLabel,
  runConsumptionHeight,
  runConsumptionOption,
  stageLabel,
} from "./tokenCharts";
import { TOKEN_HEATMAP_COLUMNS, type TokenHeatmapColumn } from "./types";
import type {
  DashboardDataSource,
  DashboardFilters,
  DashboardRunItem,
  SourceObservation,
  TokenUsageData,
  TokenUsageRow,
} from "./types";

interface RunTokenConsumptionProps {
  coordinator: RefreshCoordinator;
  dataSource: DashboardDataSource;
  datasetKey: string;
  filters: DashboardFilters;
  selectedRun: DashboardRunItem | null;
  theme: "light" | "dark";
}

interface RunTokenState {
  status: "idle" | "loading" | "ready" | "error" | "unsupported";
  error: string | null;
  observation?: SourceObservation<TokenUsageData>;
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The selected-run token read failed.";
}

function sameRun(data: TokenUsageData, run: DashboardRunItem, filters: DashboardFilters): boolean {
  return data.scope === "direct"
    && data.selectedRun?.runId === run.runId
    && Date.parse(data.filters.fromUtc) === Date.parse(filters.fromUtc)
    && Date.parse(data.filters.toUtc) === Date.parse(filters.toUtc)
    && data.filters.timeZone === filters.timeZone
    && data.filters.projectName === (filters.projectName ?? null);
}

function counterLabel(name: string): string {
  if ((TOKEN_HEATMAP_COLUMNS as readonly string[]).includes(name)) {
    return heatmapColumnLabel(name as TokenHeatmapColumn);
  }
  return nativeCategoryLabel(name);
}

function RowValues({ row }: { row: TokenUsageRow }): React.JSX.Element {
  return (
    <article className="token-detail-row">
      <h3>{configuredModelLabel(row.provider, row.configuredModel)} · {roleLabel(row.role)}</h3>
      <dl>
        <div><dt>Stage</dt><dd>{stageLabel(row.stage)}</dd></div>
        <div><dt>Coverage</dt><dd>{coverageLabel(row.coverage)}</dd></div>
        <div><dt>Known total</dt><dd>{formatTokenExact(row.knownTotalTokens)}</dd></div>
        <div><dt>Attempts</dt><dd>{row.knownAttempts} known, {row.partialAttempts} partial, {row.unknownAttempts} unknown</dd></div>
      </dl>
      <h4>Disjoint leaves</h4>
      <ul>
        {row.flows.map((flow) => (
          <li key={flow.name}>{flowLabel(flow.name)}: {formatTokenExact(flow.knownTotal)}</li>
        ))}
      </ul>
      <h4>Normalized counters</h4>
      <ul>
        {Object.entries(row.categories).map(([name, category]) => (
          <li key={name}>
            {counterLabel(name)}: {formatTokenExact(category.knownTotal)} · {coverageLabel(category.coverage)}
          </li>
        ))}
      </ul>
      <h4>Native counters</h4>
      <ul>
        {Object.entries(row.nativeCategories).map(([name, category]) => (
          <li key={name}>{nativeCategoryLabel(name)}: {formatTokenExact(category.knownTotal)}</li>
        ))}
      </ul>
    </article>
  );
}

export function RunTokenConsumption({
  coordinator,
  dataSource,
  datasetKey,
  filters,
  selectedRun,
  theme,
}: RunTokenConsumptionProps): React.JSX.Element {
  const [state, setState] = useState<RunTokenState>({ status: "idle", error: null });
  const generation = useRef(0);
  const retained = useRef<SourceObservation<TokenUsageData> | undefined>(undefined);
  const runId = selectedRun?.runId ?? null;
  const key = useMemo(
    () => canonicalReadKey("dashboard.token-usage.run", {
      fromUtc: filters.fromUtc,
      toUtc: filters.toUtc,
      timeZone: filters.timeZone,
      projectName: filters.projectName ?? null,
      runId,
      datasetKey,
    }),
    [datasetKey, filters, runId],
  );

  useEffect(() => {
    if (!selectedRun) {
      retained.current = undefined;
      setState({ status: "idle", error: null });
      return;
    }
    const ownedRun = selectedRun;
    const compatible = retained.current !== undefined && sameRun(retained.current.data, ownedRun, filters);
    if (!compatible) retained.current = undefined;
    const ownedGeneration = ++generation.current;
    setState({
      status: "loading",
      error: null,
      observation: compatible ? retained.current : undefined,
    });
    let retire = () => {};
    retire = coordinator.observe({
      key,
      cadenceMs: SELECTED_RUN_REFRESH_MS,
      read: (signal) => dataSource.tokenUsage(filters, ownedRun.runId, signal),
      onSuccess: (observation) => {
        if (ownedGeneration !== generation.current) return;
        if (!sameRun(observation.data, ownedRun, filters)) {
          setState({
            status: "error",
            error: "The token response did not match the selected run.",
            observation: retained.current,
          });
          return;
        }
        retained.current = observation;
        setState({ status: "ready", error: null, observation });
      },
      onError: (error) => {
        if (ownedGeneration !== generation.current) return;
        if (isUnsupportedTokenUsage(error)) {
          retained.current = undefined;
          setState({ status: "unsupported", error: null });
          retire();
          return;
        }
        setState({
          status: "error",
          error: safeError(error),
          observation: retained.current,
        });
      },
    });
    return () => retire();
  }, [coordinator, dataSource, filters, key, selectedRun]);

  const data = state.observation?.data;
  const view = useMemo(
    () => (data ? projectTokenUsage(data, { model: null, role: null }) : null),
    [data],
  );
  const authoritative = data?.selectedRun;
  const hasPositive = view ? view.buckets.some((bucket) =>
    bucket.flows.some((flow) => flow.knownTotal !== null && flow.knownTotal > 0),
  ) : false;
  const showChart = authoritative !== undefined && view !== null && data !== undefined && hasPositive;

  return (
    <section
      className="panel token-panel run-token-panel"
      aria-labelledby="run-token-heading"
      data-run-token-status={state.status}
    >
      <div className="panel-heading">
        <h2 id="run-token-heading">Selected run consumption</h2>
        <InformationControl label="Selected run consumption">
          <p>This chart is the selected run&apos;s direct consumption. Recovery ancestry is not added. Global model and role filters do not change it.</p>
          <p>Segments are the producer&apos;s disjoint leaves for each configured model and role. Unknown consumption stays unknown. Precise counters stay in the closed disclosure.</p>
        </InformationControl>
      </div>
      <div className="run-token-frame">
        <div className="run-token-meta">
          {state.status === "idle" ? (
            <p className="token-state">Select a run to see its direct token consumption.</p>
          ) : null}
          {state.status === "unsupported" ? (
            <p className="token-state" role="status">
              Direct token usage is not available from this dashboard service.
            </p>
          ) : null}
          {state.status === "error" ? (
            <p className="error-banner" role="alert">
              Selected-run token read: {state.error}
              <button type="button" onClick={() => coordinator.refresh(key)}>Retry token read</button>
            </p>
          ) : null}
          {state.status === "loading" && !data ? (
            <p className="loading-line" role="status">
              Reading direct token usage for {selectedRun?.shortName ?? "the selected run"}…
            </p>
          ) : null}
          {authoritative && view && data ? (
            <>
              <p className="token-known-total">
                <strong>{authoritative.shortName}</strong>
                {" · known "}
                <strong data-run-token-total={view.knownTotalTokens ?? ""}>
                  {formatTokenExact(view.knownTotalTokens)}
                </strong>
                {" · "}{coverageLabel(view.coverage)}
                {" · "}{authoritative.projectName}
              </p>
              <p className="panel-load-count">
                Observed {formatInstant(data.observedAt, filters.timeZone)}. Direct scope only.
              </p>
              {view.knownTotalTokens === null ? <p className="token-state">Token amounts are unknown.</p> : null}
              {view.knownTotalTokens === 0 ? <p className="token-state">Recorded token total is 0.</p> : null}
              {view.coverage === "partial" ? (
                <p className="token-state">Partial lower bound. Known amounts omit unrecorded consumption.</p>
              ) : null}
            </>
          ) : null}
        </div>
        <div className="run-token-chart-scroll">
          {showChart && authoritative && view ? (
            <DashboardChart
              ariaLabel={`Direct token consumption for ${authoritative.shortName}`}
              height={runConsumptionHeight(view.buckets.length)}
              option={runConsumptionOption(view)}
              ownershipKey={key}
              theme={theme}
            />
          ) : (
            <div className="token-chart-placeholder">
              <p>
                {state.status === "ready"
                  ? "No known disjoint token leaves for this run."
                  : "Direct token consumption for the selected run appears here."}
              </p>
            </div>
          )}
        </div>
        <div className="run-token-disclosure-slot">
          {authoritative && view && data ? (
            <details className="token-details-disclosure">
              <summary>Precise values</summary>
              <div className="token-detail-human">
                <dl>
                  <div><dt>Short name</dt><dd>{authoritative.shortName}</dd></div>
                  <div><dt>Project</dt><dd>{authoritative.projectName}</dd></div>
                  <div><dt>Coverage</dt><dd>{coverageLabel(data.source.coverage)}</dd></div>
                  <div><dt>Known total</dt><dd>{formatTokenExact(data.source.knownTotalTokens)}</dd></div>
                  <div><dt>Observed</dt><dd>{formatInstant(data.observedAt, filters.timeZone)}</dd></div>
                </dl>
                {data.rows.map((row) => (
                  <RowValues
                    key={`${row.provider}:${row.configuredModel ?? ""}:${row.role}:${row.stage}`}
                    row={row}
                  />
                ))}
                <dl className="token-detail-ids">
                  <div><dt>Run ID</dt><dd>{authoritative.runId}</dd></div>
                </dl>
              </div>
            </details>
          ) : null}
        </div>
      </div>
    </section>
  );
}
