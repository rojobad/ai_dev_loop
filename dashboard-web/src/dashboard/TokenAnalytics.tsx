import type { EChartsCoreOption } from "echarts/core";
import React, { useEffect, useMemo, useRef, useState } from "react";

import { DashboardChart } from "./DashboardChart";
import { InformationControl } from "./InformationControl";
import { TokenModelSelect } from "./TokenModelSelect";
import {
  SUMMARY_REFRESH_MS,
  type RefreshCoordinator,
  canonicalReadKey,
} from "./refreshCoordinator";
import { formatInstant } from "./time";
import {
  EMPTY_TOKEN_FACETS,
  configuredModelLabel,
  coverageLabel,
  facetsFromSelection,
  formatTokenExact,
  modelFacetFromKey,
  modelFacetKey,
  positiveFlowTotal,
  isUnsupportedTokenUsage,
  projectTokenUsage,
  reconcileTokenFacets,
  roleLabel,
  tokenHeatmapHeight,
  tokenHeatmapOption,
  tokenSankeyHeight,
  tokenSankeyOption,
  tokenSelectionFromChartEvent,
  tokenSunburstOption,
  type TokenFacets,
  type TokenUsageView,
} from "./tokenCharts";
import type {
  DashboardDataSource,
  DashboardFilters,
  SourceObservation,
  TokenRoleName,
  TokenUsageData,
} from "./types";

interface TokenAnalyticsProps {
  coordinator: RefreshCoordinator;
  dataSource: DashboardDataSource;
  datasetKey: string;
  filters: DashboardFilters;
  theme: "light" | "dark";
}

interface TokenState {
  status: "loading" | "ready" | "error" | "unsupported";
  error: string | null;
  observation?: SourceObservation<TokenUsageData>;
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The token read failed.";
}

function sameTokenScope(data: TokenUsageData, filters: DashboardFilters): boolean {
  return Date.parse(data.filters.fromUtc) === Date.parse(filters.fromUtc)
    && Date.parse(data.filters.toUtc) === Date.parse(filters.toUtc)
    && data.filters.timeZone === filters.timeZone
    && data.filters.projectName === (filters.projectName ?? null);
}

function viewMessage(data: TokenUsageData, view: TokenUsageView): string | null {
  if (!view.filtered && data.cohortRunCount === 0) return "No started runs in this scope.";
  if (view.knownTotalTokens === null && view.coverage === "unknown") {
    return "Token amounts are unknown.";
  }
  if (view.knownTotalTokens === null) return "No known token consumption for this selection.";
  if (view.knownTotalTokens === 0) return "Recorded token total is 0.";
  if (view.coverage === "partial") {
    return "Partial lower bound. Known amounts omit unrecorded consumption.";
  }
  return null;
}

function TokenChartPanel({
  title,
  label,
  headingId,
  explanation,
  option,
  height,
  ownershipKey,
  theme,
  onSelect,
  message,
  frameClassName,
}: {
  title: string;
  label: string;
  headingId: string;
  explanation: React.ReactNode;
  option: EChartsCoreOption | null;
  height: number;
  ownershipKey: string;
  theme: "light" | "dark";
  onSelect: (value: unknown) => void;
  message: string | null;
  frameClassName?: string;
}): React.JSX.Element {
  const chart = option ? (
    <DashboardChart
      ariaLabel={label}
      height={height}
      onEvents={{ click: onSelect }}
      option={option}
      ownershipKey={ownershipKey}
      theme={theme}
    />
  ) : (
    <div className="token-chart-placeholder" style={{ minHeight: Math.min(height, 480) }}>
      <p>{message ?? "No known token flow for this selection."}</p>
    </div>
  );
  return (
    <section className="panel token-panel" aria-labelledby={headingId}>
      <div className="panel-heading">
        <h2 id={headingId}>{title}</h2>
        <InformationControl label={label}>{explanation}</InformationControl>
      </div>
      {frameClassName ? <div className={frameClassName}>{chart}</div> : chart}
    </section>
  );
}

export function TokenAnalytics({
  coordinator,
  dataSource,
  datasetKey,
  filters,
  theme,
}: TokenAnalyticsProps): React.JSX.Element {
  const [state, setState] = useState<TokenState>({ status: "loading", error: null });
  const [facets, setFacets] = useState<TokenFacets>(EMPTY_TOKEN_FACETS);
  const generation = useRef(0);
  const retained = useRef<SourceObservation<TokenUsageData> | undefined>(undefined);
  const key = useMemo(
    () => canonicalReadKey("dashboard.token-usage", {
      fromUtc: filters.fromUtc,
      toUtc: filters.toUtc,
      timeZone: filters.timeZone,
      projectName: filters.projectName ?? null,
      datasetKey,
    }),
    [datasetKey, filters],
  );

  useEffect(() => {
    const ownedGeneration = ++generation.current;
    const compatible = retained.current !== undefined && sameTokenScope(retained.current.data, filters);
    if (!compatible) {
      retained.current = undefined;
      setFacets(EMPTY_TOKEN_FACETS);
    }
    setState({
      status: "loading",
      error: null,
      observation: compatible ? retained.current : undefined,
    });
    let retire = () => {};
    retire = coordinator.observe({
      key,
      cadenceMs: SUMMARY_REFRESH_MS,
      read: (signal) => dataSource.tokenUsage(filters, null, signal),
      onSuccess: (observation) => {
        if (ownedGeneration !== generation.current) return;
        if (observation.data.scope !== "cohort" || !sameTokenScope(observation.data, filters)) {
          setState({
            status: "error",
            error: "The token response did not match the applied scope.",
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
          setFacets(EMPTY_TOKEN_FACETS);
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
  }, [coordinator, dataSource, filters, key]);

  useEffect(() => {
    const rows = state.observation?.data.rows;
    if (!rows) return;
    setFacets((current) => reconcileTokenFacets(current, rows));
  }, [state.observation]);

  const data = state.observation?.data;
  const view = useMemo(
    () => (data && state.status !== "unsupported" ? projectTokenUsage(data, facets) : null),
    [data, facets, state.status],
  );
  const message = state.status === "unsupported"
    ? "Token usage is not available from this dashboard service."
    : data && view
      ? viewMessage(data, view)
      : state.status === "loading"
        ? "Reading token usage…"
        : null;
  const showFlow = view !== null && positiveFlowTotal(view.flows) > 0;
  const showHeatmap = view !== null && view.heatmap.length > 0;
  const onSelect = (value: unknown) => {
    const target = tokenSelectionFromChartEvent(value);
    if (target) setFacets((current) => facetsFromSelection(target, current));
  };
  const modelOptions = useMemo(() => {
    const options = new Map<string, string>();
    for (const row of data?.rows ?? []) {
      const facet = { provider: row.provider, configuredModel: row.configuredModel };
      options.set(modelFacetKey(facet), configuredModelLabel(row.provider, row.configuredModel));
    }
    return [...options.entries()];
  }, [data?.rows]);

  const flowExplanation = (
    <>
      <p>Models are the configured model on the authenticated invocation, qualified by provider. The same spelling on Cursor and Codex stays separate. An unrecorded model stays unrecorded.</p>
      <p>Widths use the producer&apos;s disjoint leaves for runs whose first execution launch is in the applied scope. Later direct usage of those runs is included. Cache and reasoning are counted once. Unknown amounts have no width.</p>
      <p>A partial total is a lower bound. Model and role selection filters these three charts locally and does not change Inspection scope, the run map, or the selected run.</p>
      <p>Choose several models using the checkboxes. With no specific models selected, all models are included. Clicking a model in a chart toggles it; a role or leaf adds its model and applies that role. Clear selection resets both filters.</p>
    </>
  );
  const observed = state.observation
    ? `Observed ${formatInstant(state.observation.data.observedAt, filters.timeZone)}.`
    : null;

  return (
    <section
      className="token-analytics"
      aria-labelledby="token-analytics-heading"
      data-token-status={state.status}
    >
      <section className="panel token-scope" aria-labelledby="token-analytics-heading">
        <div className="panel-heading">
          <h2 id="token-analytics-heading">Token Scope</h2>
          <InformationControl label="Token Scope">
            {flowExplanation}
            {observed ? <p>{observed}</p> : null}
            {data ? <p>{data.cohortRunCount} started runs in Inspection scope.</p> : null}
            {data && view ? <>
              {message ? <p>{message}</p> : null}
              {view.diagnostics.length ? (
                <p>Usage diagnostics: {view.diagnostics.join(", ")}.</p>
              ) : null}
            </> : null}
          </InformationControl>
        </div>
        {state.status === "unsupported" ? (
          <p className="token-state" role="status">
            Token usage is not available from this dashboard service. Overview, map, timeline, and activity stay available.
          </p>
        ) : null}
        {state.status === "error" ? (
          <p className="error-banner" role="alert">
            Token read: {state.error}
            <button type="button" onClick={() => coordinator.refresh(key)}>Retry token read</button>
          </p>
        ) : null}
        {state.status === "loading" && !data ? (
          <p className="loading-line" role="status">Reading token usage…</p>
        ) : null}
        {data && view ? (
          <>
            <p
              className="token-known-total panel-load-count"
              data-token-observed-at={data.observedAt}
              data-token-project={data.filters.projectName ?? ""}
            >
              Known total <strong>{formatTokenExact(view.knownTotalTokens)}</strong>
              {" · "}{coverageLabel(view.coverage)}
            </p>
            {message && view.coverage !== "partial" ? <p className="token-state">{message}</p> : null}
            <div className="token-facets">
              <TokenModelSelect
                options={modelOptions}
                selected={facets.models.map(modelFacetKey)}
                onChange={(keys) => {
                  const models = keys.flatMap((key) => {
                    const model = modelFacetFromKey(key);
                    return model ? [model] : [];
                  });
                  setFacets((current) => ({ ...current, models }));
                }}
              />
              <label>
                Role
                <select
                  aria-label="Token role"
                  value={facets.role ?? ""}
                  onChange={(event) => {
                    const role = event.target.value;
                    setFacets((current) => ({
                      ...current,
                      role: role === "implementer" || role === "reviewer" ? role as TokenRoleName : null,
                    }));
                  }}
                >
                  <option value="">All roles</option>
                  <option value="implementer">{roleLabel("implementer")}</option>
                  <option value="reviewer">{roleLabel("reviewer")}</option>
                </select>
              </label>
              <button type="button" onClick={() => setFacets(EMPTY_TOKEN_FACETS)}>
                Clear selection
              </button>
            </div>
          </>
        ) : null}
      </section>
      <div className="token-sankey-slot">
        <TokenChartPanel
          title="Token flow"
          label="Token flow"
          headingId="token-flow-heading"
          explanation={flowExplanation}
          option={showFlow && view ? tokenSankeyOption(view) : null}
          height={view ? tokenSankeyHeight(view) : 480}
          ownershipKey={`${key}:sankey`}
          theme={theme}
          onSelect={onSelect}
          message={message}
          frameClassName="token-sankey-scroll"
        />
      </div>
      <div className="token-pair">
        <TokenChartPanel
          title="Token composition"
          label="Token composition"
          headingId="token-composition-heading"
          explanation={(
            <>
              <p>Rings keep the order configured model, role, then disjoint token leaf. The center repeats the known total. Leaf colors match Token flow.</p>
              <p>Selecting a model sector toggles that model across all three charts. A role or leaf adds its model and applies its role, keeping other model choices. It does not convert a leaf into an overlapping counter.</p>
            </>
          )}
          option={showFlow && view ? tokenSunburstOption(view) : null}
          height={420}
          ownershipKey={`${key}:sunburst`}
          theme={theme}
          onSelect={onSelect}
          message={message}
        />
        <section className="panel token-panel" aria-labelledby="token-categories-heading">
          <div className="panel-heading">
            <h2 id="token-categories-heading">Token categories</h2>
            <InformationControl label="Token categories">
              <p>Columns are normalized counters and can overlap. Do not add input, cache, output, and reasoning together. They are not the disjoint leaves used by Token flow.</p>
              <p>An em dash is an absent or unknown cell. A measured zero stays zero. A partial cell is a lower bound, marked by its border.</p>
            </InformationControl>
          </div>
          {showHeatmap && view ? (
            <div className="token-heatmap-scroll">
              <DashboardChart
                ariaLabel="Token categories"
                height={tokenHeatmapHeight(view.heatmap.length)}
                onEvents={{ click: onSelect }}
                option={tokenHeatmapOption(view)}
                ownershipKey={`${key}:heatmap`}
                theme={theme}
              />
            </div>
          ) : (
            <div className="token-chart-placeholder" style={{ minHeight: 360 }}>
              <p>{message ?? "No token categories for this selection."}</p>
            </div>
          )}
        </section>
      </div>
    </section>
  );
}
