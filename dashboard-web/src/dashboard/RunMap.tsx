import type { CustomSeriesRenderItem, EChartsCoreOption } from "echarts";
import React, { useEffect, useMemo, useRef, useState } from "react";

import { DashboardChart } from "./DashboardChart";
import { escapeHtml } from "./charts";
import { SUMMARY_REFRESH_MS, RefreshCoordinator, canonicalReadKey } from "./refreshCoordinator";
import type {
  DashboardCoverage,
  DashboardDataSource,
  DashboardFilters,
  DashboardPage,
  DashboardRunItem,
  SourceObservation,
} from "./types";

const RUN_PAGE_SIZE = 25;
const RUN_CELL_STEP_PX = 44;
const RUN_MAP_AXIS_LEFT_PX = 220;
const RUN_MAP_AXIS_RIGHT_PX = 24;

interface RunMapState {
  items: DashboardRunItem[];
  totalCount: number;
  coverage?: DashboardCoverage;
  page?: DashboardPage;
  firstPage?: DashboardPage;
  observations: PageObservation[];
  loading: boolean;
  error: string | null;
  firstPageIds: string[];
}

interface PageObservation {
  offset: number;
  observedAt: string;
  receivedAt: string;
}

export interface RunMapProps {
  dataSource: DashboardDataSource;
  coordinator: RefreshCoordinator;
  filters: DashboardFilters;
  datasetKey: string;
  selectedRunId: string | null;
  onSelectRun: (run: DashboardRunItem) => void;
  theme: "light" | "dark";
}

interface RunMapGroup {
  key: string;
  label: string;
  items: DashboardRunItem[];
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The run map read failed.";
}

function observationAge(receivedAt: string): string {
  const milliseconds = Math.max(0, Date.now() - Date.parse(receivedAt));
  if (milliseconds < 60_000) return `${Math.floor(milliseconds / 1000)}s old`;
  if (milliseconds < 3_600_000) return `${Math.floor(milliseconds / 60_000)}m old`;
  return `${Math.floor(milliseconds / 3_600_000)}h old`;
}

function sameFilterEcho(filters: DashboardFilters, observation: SourceObservation<unknown> & {
  data: { filters: { fromUtc: string; toUtc: string; timeZone: string; projectName: string | null } };
}): boolean {
  const echo = observation.data.filters;
  const sameInstant = (left: string, right: string) => Date.parse(left) === Date.parse(right);
  return (
    sameInstant(echo.fromUtc, filters.fromUtc) &&
    sameInstant(echo.toUtc, filters.toUtc) &&
    echo.timeZone === filters.timeZone &&
    echo.projectName === (filters.projectName ?? null)
  );
}

function mergeRuns(current: DashboardRunItem[], incoming: DashboardRunItem[]): DashboardRunItem[] {
  const byId = new Map(current.map((item) => [item.runId, item]));
  for (const item of incoming) byId.set(item.runId, item);
  return [...byId.values()];
}

function recordPageObservation(
  current: PageObservation[],
  offset: number,
  observation: SourceObservation<unknown>,
): PageObservation[] {
  return [
    ...current.filter((item) => item.offset !== offset),
    { offset, observedAt: observation.observedAt, receivedAt: observation.receivedAt },
  ].sort((left, right) => left.offset - right.offset);
}

export function groupRunMapItems(items: DashboardRunItem[]): RunMapGroup[] {
  const sequences = new Map<string, DashboardRunItem[]>();
  const standalone: DashboardRunItem[] = [];
  for (const item of items) {
    if (item.sequenceBinding === null) {
      standalone.push(item);
      continue;
    }
    const grouped = sequences.get(item.sequenceBinding.sequenceId) ?? [];
    grouped.push(item);
    sequences.set(item.sequenceBinding.sequenceId, grouped);
  }
  const result = [...sequences.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([sequenceId, runs]) => {
      const sorted = [...runs].sort((left, right) => {
        const leftBinding = left.sequenceBinding;
        const rightBinding = right.sequenceBinding;
        if (leftBinding === null || rightBinding === null) return left.runId.localeCompare(right.runId);
        return (
          leftBinding.ordinal - rightBinding.ordinal ||
          leftBinding.generation - rightBinding.generation ||
          left.runId.localeCompare(right.runId)
        );
      });
      return {
        key: sequenceId,
        label: `${sorted[0]?.sequenceBinding?.sequenceName ?? "Sequence"} · ${sequenceId}`,
        items: sorted,
      };
    });
  if (standalone.length) {
    result.push({
      key: "standalone",
      label: "Standalone runs",
      items: [...standalone].sort((left, right) =>
        left.firstExecutionLaunchAt.localeCompare(right.firstExecutionLaunchAt) ||
        left.runId.localeCompare(right.runId),
      ),
    });
  }
  return result;
}

const renderRunCell: CustomSeriesRenderItem = (_params, api) => {
  const point = api.coord([Number(api.value(0)), Number(api.value(1))]);
  const rawSize = api.size?.([0.72, 0.72]) ?? [18, 18];
  const size = Array.isArray(rawSize) ? rawSize : [rawSize, rawSize];
  return {
    type: "rect",
    shape: {
      x: point[0] - Math.max(8, Math.abs(size[0]) / 2),
      y: point[1] - Math.max(7, Math.abs(size[1]) / 3),
      width: Math.max(16, Math.abs(size[0])),
      height: Math.max(14, Math.abs(size[1]) * 0.66),
      r: 3,
    },
    style: api.style(),
  };
};

export function runMapOption(items: DashboardRunItem[]): EChartsCoreOption {
  const groups = groupRunMapItems(items);
  const groupIndex = new Map(groups.map((group, index) => [group.key, index]));
  const positionByRunId = new Map<string, number>();
  for (const group of groups) {
    group.items.forEach((item, index) => positionByRunId.set(item.runId, index + 1));
  }
  const maxColumns = Math.max(1, ...groups.map((group) => group.items.length));
  const data = items.map((item) => {
    const binding = item.sequenceBinding;
    const groupKey = binding?.sequenceId ?? "standalone";
    const x = positionByRunId.get(item.runId) ?? 1;
    return {
      value: [x, groupIndex.get(groupKey) ?? 0],
      runId: item.runId,
      projectName: item.projectName,
      state: item.state,
      provider: item.implementerProvider,
      phase: binding ? `${binding.ordinal}. ${binding.phaseName}` : "Standalone",
      generation: binding?.generation ?? null,
      attemptKind: binding?.attemptKind ?? null,
      itemStyle: { color: item.implementerProvider === "codex" ? "#8054ae" : "#2f76d2" },
    };
  });
  return {
    animation: false,
    grid: {
      left: RUN_MAP_AXIS_LEFT_PX,
      right: RUN_MAP_AXIS_RIGHT_PX,
      top: 18,
      bottom: 54,
      containLabel: false,
    },
    tooltip: {
      renderMode: "html",
      formatter: (value: unknown) => {
        const event = typeof value === "object" && value !== null
          ? (value as Record<string, unknown>)
          : {};
        const item = typeof event.data === "object" && event.data !== null
          ? (event.data as Record<string, unknown>)
          : {};
        const runId = typeof item.runId === "string" ? item.runId : "Run";
        const phase = typeof item.phase === "string" ? item.phase : "Unknown phase";
        const state = typeof item.state === "string" ? item.state : "unknown";
        const provider = typeof item.provider === "string" ? item.provider : "unknown";
        return `<strong>${escapeHtml(runId)}</strong><br>${escapeHtml(phase)}<br>${escapeHtml(provider)} · ${escapeHtml(state)}`;
      },
    },
    xAxis: {
      type: "value",
      min: 0.5,
      max: maxColumns + 0.5,
      minInterval: 1,
      name: "Distinct run position within group",
    },
    yAxis: {
      type: "category",
      data: groups.map((group) => group.label),
      axisLabel: { width: 190, overflow: "truncate" },
    },
    series: [{ type: "custom", renderItem: renderRunCell, data }],
  };
}

export function runIdFromChartEvent(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const event = value as Record<string, unknown>;
  if (typeof event.data !== "object" || event.data === null) return null;
  const runId = (event.data as Record<string, unknown>).runId;
  return typeof runId === "string" && runId.length > 0 ? runId : null;
}

export function RunMap({
  dataSource,
  coordinator,
  filters,
  datasetKey,
  selectedRunId,
  onSelectRun,
  theme,
}: RunMapProps): React.JSX.Element {
  const [state, setState] = useState<RunMapState>({
    items: [],
    totalCount: 0,
    observations: [],
    loading: true,
    error: null,
    firstPageIds: [],
  });
  const generation = useRef(0);
  const pageController = useRef<AbortController | null>(null);
  const rowButtons = useRef(new Map<string, HTMLButtonElement>());

  async function loadPage(offset: number, requestGeneration: number) {
    pageController.current?.abort();
    const controller = new AbortController();
    pageController.current = controller;
    setState((current) => ({ ...current, loading: true, error: null }));
    try {
      const observation = await coordinator.request(
        canonicalReadKey("dashboard.runs.page", { datasetKey, offset, limit: RUN_PAGE_SIZE }),
        (signal) => dataSource.runs(filters, { offset, limit: RUN_PAGE_SIZE }, signal),
        controller.signal,
      );
      if (controller.signal.aborted || requestGeneration !== generation.current) return;
      if (!sameFilterEcho(filters, observation)) {
        throw new Error("The run map response did not match the applied filter scope.");
      }
      setState((current) => ({
        items: mergeRuns(current.items, observation.data.items),
        totalCount: observation.data.totalCount,
        coverage: observation.data.coverage,
        page: observation.data.page,
        firstPage: current.firstPage,
        observations: recordPageObservation(current.observations, offset, observation),
        loading: false,
        error: null,
        firstPageIds: current.firstPageIds,
      }));
    } catch (error) {
      if (!controller.signal.aborted && requestGeneration === generation.current) {
        setState((current) => ({ ...current, loading: false, error: safeError(error) }));
      }
    } finally {
      if (pageController.current === controller) pageController.current = null;
    }
  }

  const scopeKey = useMemo(
    () =>
      canonicalReadKey("dashboard.runs.scope", {
        fromUtc: new Date(filters.fromUtc).toISOString(),
        toUtc: new Date(filters.toUtc).toISOString(),
        timeZone: filters.timeZone,
        projectName: filters.projectName ?? null,
      }),
    [filters],
  );
  const previousScopeKey = useRef<string | null>(null);

  useEffect(() => {
    const requestGeneration = ++generation.current;
    pageController.current?.abort();
    pageController.current = null;
    const compatibleScope = previousScopeKey.current === scopeKey;
    previousScopeKey.current = scopeKey;
    setState((current) => {
      if (!compatibleScope) {
        return {
          items: [],
          totalCount: 0,
          observations: [],
          loading: true,
          error: null,
          firstPageIds: [],
        };
      }
      const firstPageIds = new Set(current.firstPageIds);
      return {
        ...current,
        items: current.items.filter((item) => firstPageIds.has(item.runId)),
        page: current.firstPage,
        observations: current.observations.filter((item) => item.offset === 0),
        loading: true,
        error: null,
      };
    });
    const retire = coordinator.observe({
      key: canonicalReadKey("dashboard.runs", { datasetKey, offset: 0, limit: RUN_PAGE_SIZE }),
      cadenceMs: SUMMARY_REFRESH_MS,
      read: (signal) => dataSource.runs(filters, { offset: 0, limit: RUN_PAGE_SIZE }, signal),
      onSuccess: (observation) => {
        if (requestGeneration !== generation.current) return;
        if (!sameFilterEcho(filters, observation)) {
          setState((current) => ({
            ...current,
            loading: false,
            error: "The run map response did not match the applied filter scope.",
          }));
          return;
        }
        setState((current) => {
          const previousFirstPage = new Set(current.firstPageIds);
          const retainedHistoricPages = current.items.filter(
            (item) => !previousFirstPage.has(item.runId),
          );
          const firstPageIds = observation.data.items.map((item) => item.runId);
          return {
            items: mergeRuns(observation.data.items, retainedHistoricPages),
            totalCount: observation.data.totalCount,
            coverage: observation.data.coverage,
            page:
              current.page && current.page.offset > 0
                ? current.page
                : observation.data.page,
            firstPage: observation.data.page,
            observations: recordPageObservation(current.observations, 0, observation),
            loading: false,
            error: null,
            firstPageIds,
          };
        });
      },
      onError: (error) => {
        if (requestGeneration === generation.current) {
          setState((current) => ({ ...current, loading: false, error: safeError(error) }));
        }
      },
    });
    return () => {
      retire();
      pageController.current?.abort();
      pageController.current = null;
    };
  }, [coordinator, dataSource, datasetKey, filters, scopeKey]);

  useEffect(() => () => pageController.current?.abort(), []);

  const groups = useMemo(() => groupRunMapItems(state.items), [state.items]);
  const option = useMemo(() => runMapOption(state.items), [state.items]);
  const chartMinWidth = useMemo(
    () =>
      Math.max(
        640,
        Math.max(1, ...groups.map((group) => group.items.length)) * RUN_CELL_STEP_PX +
          RUN_MAP_AXIS_LEFT_PX +
          RUN_MAP_AXIS_RIGHT_PX,
      ),
    [groups],
  );

  const select = (runId: string, focusCompanion: boolean) => {
    const run = state.items.find((item) => item.runId === runId);
    if (!run) return;
    onSelectRun(run);
    if (focusCompanion) {
      requestAnimationFrame(() => {
        const button = rowButtons.current.get(runId);
        button?.focus({ preventScroll: true });
        button?.scrollIntoView?.({ block: "nearest" });
      });
    }
  };

  const chartEvents = useMemo(
    () => ({
      click: (event: unknown) => {
        const runId = runIdFromChartEvent(event);
        if (runId) select(runId, true);
      },
    }),
    // The item list is replaced on each page response; exact IDs fence selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.items, onSelectRun],
  );

  return (
    <section className="panel run-map-panel" aria-labelledby="map-heading">
      <div className="panel-heading">
        <div>
          <h2 id="map-heading">Sequence and run map</h2>
          <p>Every loaded cell is one authoritative run ID; replacements are never collapsed.</p>
        </div>
        <strong aria-live="polite">
          Loaded {state.items.length} of {state.totalCount} started runs
        </strong>
      </div>
      {state.coverage ? (
        <p className={`coverage coverage-${state.coverage.dataState}`}>
          Dataset coverage: {state.coverage.dataState}
          {state.coverage.warnings.length ? ` · ${state.coverage.warnings.join(", ")}` : ""}
        </p>
      ) : null}
      {state.observations.length ? (
        <p className="observation-line">
          Page observations: {state.observations.map((item) =>
            `offset ${item.offset} · ${item.observedAt} received ${item.receivedAt} (${observationAge(item.receivedAt)})`
          ).join(" · ")} (fresh offset pages; not one atomic snapshot)
        </p>
      ) : null}
      {state.error ? (
        <p className="error-banner" role="alert">
          Run map: {state.error}
          {state.observations.length
            ? ` · showing the last successful visible pages (${observationAge(state.observations[0].receivedAt)})`
            : ""}
        </p>
      ) : null}
      {state.items.length ? (
        <>
          <div className="run-map-chart-scroll">
            <div style={{ minWidth: chartMinWidth }}>
              <DashboardChart
                ariaLabel="Selectable chart cells for authoritative run IDs"
                height={Math.max(220, groups.length * 72)}
                onEvents={chartEvents}
                option={option}
                ownershipKey={datasetKey}
                theme={theme}
              />
            </div>
          </div>
          <div className="run-map-groups" aria-label="Keyboard-selectable run map rows">
            {groups.map((group) => (
              <section key={group.key} className="run-map-group" aria-label={group.label}>
                <h3>{group.label}</h3>
                <div className="run-map-rows">
                  {group.items.map((run) => {
                    const binding = run.sequenceBinding;
                    return (
                      <button
                        type="button"
                        key={run.runId}
                        ref={(node) => {
                          if (node) rowButtons.current.set(run.runId, node);
                          else rowButtons.current.delete(run.runId);
                        }}
                        aria-pressed={selectedRunId === run.runId}
                        onClick={() => select(run.runId, false)}
                      >
                        <span className="run-row-title">{run.runId}</span>
                        <span>{run.implementerProvider} implementer · {run.state}</span>
                        <span>
                          {binding
                            ? `Phase ${binding.ordinal} ${binding.phaseName} · generation ${binding.generation} · ${binding.attemptKind}`
                            : "Standalone"}
                        </span>
                        {binding?.sourceRunId ? <span>Replaces {binding.sourceRunId}</span> : null}
                      </button>
                    );
                  })}
                </div>
              </section>
            ))}
          </div>
        </>
      ) : !state.loading && (state.observations.length > 0 || !state.error) ? (
        <div className="empty-state">No started runs match the applied scope.</div>
      ) : !state.loading ? (
        <div className="empty-state">Run data is unavailable until a successful read.</div>
      ) : null}
      {state.loading ? <p className="loading-line">Reading authoritative run page…</p> : null}
      {state.page?.hasMore && state.page.nextOffset !== null ? (
        <button
          type="button"
          disabled={state.loading}
          onClick={() => void loadPage(state.page!.nextOffset!, generation.current)}
        >
          Load more runs
        </button>
      ) : null}
    </section>
  );
}
