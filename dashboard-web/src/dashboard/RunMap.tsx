import type { CustomSeriesRenderItem, EChartsCoreOption } from "echarts";
import React, { useEffect, useMemo, useRef, useState } from "react";

import { DashboardChart } from "./DashboardChart";
import { InformationControl } from "./InformationControl";
import { escapeHtml } from "./charts";
import { SUMMARY_REFRESH_MS, RefreshCoordinator, canonicalReadKey } from "./refreshCoordinator";
import { formatAge, formatInstant } from "./time";
import { DashboardDataSourceError } from "./types";
import type {
  DashboardCoverage,
  DashboardDataSource,
  DashboardFilters,
  DashboardPage,
  DashboardRunItem,
  DashboardRunsData,
  SourceObservation,
} from "./types";

const RUN_INITIAL_PAGE_SIZE = 1000;
const RUN_CELL_STEP_PX = 44;
const RUN_MAP_AXIS_LEFT_PX = 220;
const RUN_MAP_AXIS_RIGHT_PX = 24;

interface RunMapState {
  items: DashboardRunItem[];
  totalCount: number;
  coverage?: DashboardCoverage;
  page?: DashboardPage;
  observations: PageObservation[];
  loading: boolean;
  error: string | null;
  retryPage: { offset: number; limit: number } | null;
}

interface PageObservation {
  offset: number;
  observedAt: string;
  receivedAt: string;
}

interface PendingPageChain {
  items: DashboardRunItem[];
  totalCount: number;
  coverage: DashboardCoverage;
  page: DashboardPage;
  observations: PageObservation[];
}

export interface RunMapProps {
  dataSource: DashboardDataSource;
  coordinator: RefreshCoordinator;
  filters: DashboardFilters;
  datasetKey: string;
  selectedRunId: string | null;
  onSelectRun: (run: DashboardRunItem | null) => void;
  theme: "light" | "dark";
}

interface RunMapGroup {
  key: string;
  label: string;
  details: string | null;
  items: DashboardRunItem[];
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The run map read failed.";
}

function isExpectedCancellation(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
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

export async function readAdaptiveRunsPage(
  dataSource: DashboardDataSource,
  filters: DashboardFilters,
  offset: number,
  initialLimit: number,
  signal: AbortSignal,
): Promise<SourceObservation<DashboardRunsData>> {
  let limit = initialLimit;
  for (;;) {
    try {
      const observation = await dataSource.runs(filters, { offset, limit }, signal);
      if (observation.data.page.offset !== offset || observation.data.page.limit !== limit) {
        throw new Error("The run page response changed the requested offset or limit.");
      }
      return observation;
    } catch (error) {
      if (
        error instanceof DashboardDataSourceError &&
        error.code === "PAGE_ENVELOPE_TOO_LARGE" &&
        limit > 1 &&
        !signal.aborted
      ) {
        limit = Math.max(1, Math.floor(limit / 2));
        continue;
      }
      throw error;
    }
  }
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
  const result: RunMapGroup[] = [...sequences.entries()]
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
        label: sorted[0]?.sequenceBinding?.sequenceShortName ?? "Sequence",
        details: `${sorted[0]?.sequenceBinding?.sequenceName ?? "Sequence"} · ${sequenceId}`,
        items: sorted,
      };
    });
  if (standalone.length) {
    result.push({
      key: "standalone",
      label: "Standalone runs",
      details: null,
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

export function runMapOption(
  items: DashboardRunItem[],
  timeZone = "UTC",
): EChartsCoreOption {
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
      shortName: item.shortName,
      projectName: item.projectName,
      submittedAt: formatInstant(item.submittedAt, timeZone),
      firstExecutionLaunchAt: formatInstant(item.firstExecutionLaunchAt, timeZone),
      state: item.state,
      provider: item.implementerProvider,
      phase: binding ? `${binding.ordinal}. ${binding.phaseName}` : "Standalone",
      sequenceName: binding?.sequenceName ?? null,
      sequenceId: binding?.sequenceId ?? null,
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
        const shortName = typeof item.shortName === "string" ? item.shortName : "Run";
        const phase = typeof item.phase === "string" ? item.phase : "Unknown phase";
        const projectName = typeof item.projectName === "string" ? item.projectName : "Unknown project";
        const submittedAt = typeof item.submittedAt === "string" ? item.submittedAt : "Unavailable";
        const launchedAt = typeof item.firstExecutionLaunchAt === "string"
          ? item.firstExecutionLaunchAt
          : "Unavailable";
        const sequenceName = typeof item.sequenceName === "string" ? item.sequenceName : null;
        const sequenceId = typeof item.sequenceId === "string" ? item.sequenceId : null;
        const state = typeof item.state === "string" ? item.state : "unknown";
        const provider = typeof item.provider === "string" ? item.provider : "unknown";
        const sequence = sequenceName && sequenceId
          ? `<br>${escapeHtml(sequenceName)}<br>Sequence ID: ${escapeHtml(sequenceId)}`
          : "";
        return `<strong>${escapeHtml(shortName)}</strong><br>${escapeHtml(projectName)} · ${escapeHtml(provider)} · ${escapeHtml(state)}<br>Submitted: ${escapeHtml(submittedAt)}<br>First launch: ${escapeHtml(launchedAt)}<br>${escapeHtml(phase)}${sequence}<br>Run ID: ${escapeHtml(runId)}`;
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
    retryPage: null,
  });
  const generation = useRef(0);
  const chainController = useRef<AbortController | null>(null);
  const retryContinuation = useRef<((offset: number, limit: number) => void) | null>(null);
  const previousScopeKey = useRef<string | null>(null);
  const hasSuccessfulHistory = useRef(false);

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
  const refreshOwnerKey = useMemo(
    () => canonicalReadKey("dashboard.runs", {
      datasetKey,
      offset: 0,
      limit: RUN_INITIAL_PAGE_SIZE,
    }),
    [datasetKey],
  );

  useEffect(() => {
    const requestGeneration = ++generation.current;
    chainController.current?.abort();
    chainController.current = null;
    const compatibleScope = previousScopeKey.current === scopeKey;
    previousScopeKey.current = scopeKey;
    if (!compatibleScope) hasSuccessfulHistory.current = false;
    setState((current) => compatibleScope
      ? {
          ...current,
          loading: true,
          error: null,
          retryPage: null,
        }
      : {
          items: [],
          totalCount: 0,
          observations: [],
          loading: true,
          error: null,
          retryPage: null,
        });

    const startContinuation = (
      offset: number,
      limit: number,
      pendingChain: PendingPageChain | null = null,
    ) => {
      chainController.current?.abort();
      const controller = new AbortController();
      chainController.current = controller;
      setState((current) => ({ ...current, loading: true, error: null, retryPage: null }));
      void (async () => {
        let nextOffset = offset;
        let nextLimit = limit;
        let pending = pendingChain;
        try {
          for (;;) {
            const observation = await coordinator.requestRefreshContinuation(
              refreshOwnerKey,
              canonicalReadKey("dashboard.runs.page", {
                datasetKey,
                offset: nextOffset,
                limit: nextLimit,
              }),
              (signal) => readAdaptiveRunsPage(
                dataSource,
                filters,
                nextOffset,
                nextLimit,
                signal,
              ),
              controller.signal,
            );
            if (controller.signal.aborted || requestGeneration !== generation.current) return;
            if (!sameFilterEcho(filters, observation)) {
              throw new Error("The run map response did not match the current filter scope.");
            }
            if (observation.data.items.length > 0) hasSuccessfulHistory.current = true;
            if (pending) {
              pending = {
                items: mergeRuns(pending.items, observation.data.items),
                totalCount: observation.data.totalCount,
                coverage: observation.data.coverage,
                page: observation.data.page,
                observations: recordPageObservation(
                  pending.observations,
                  observation.data.page.offset,
                  observation,
                ),
              };
              if (!observation.data.page.hasMore) {
                const completed = pending;
                hasSuccessfulHistory.current = completed.items.length > 0;
                setState((current) => ({
                  ...current,
                  ...completed,
                  loading: false,
                  error: null,
                  retryPage: null,
                }));
              } else {
                setState((current) => {
                  const visibleItems = mergeRuns(current.items, observation.data.items);
                  return {
                    ...current,
                    items: visibleItems,
                    totalCount: Math.max(observation.data.totalCount, visibleItems.length),
                    coverage: observation.data.coverage,
                    page: observation.data.page,
                    observations: recordPageObservation(
                      current.observations,
                      observation.data.page.offset,
                      observation,
                    ),
                    loading: true,
                    error: null,
                    retryPage: null,
                  };
                });
              }
            } else {
              setState((current) => ({
                ...current,
                items: mergeRuns(current.items, observation.data.items),
                totalCount: observation.data.totalCount,
                coverage: observation.data.coverage,
                page: observation.data.page,
                observations: recordPageObservation(
                  current.observations,
                  observation.data.page.offset,
                  observation,
                ),
                loading: observation.data.page.hasMore,
                error: null,
                retryPage: null,
              }));
            }
            if (!observation.data.page.hasMore) return;
            const returnedOffset = observation.data.page.nextOffset;
            if (returnedOffset === null || returnedOffset <= nextOffset) {
              throw new Error("Run map paging did not advance.");
            }
            nextOffset = returnedOffset;
            nextLimit = observation.data.page.limit;
          }
        } catch (error) {
          if (
            isExpectedCancellation(error) &&
            !controller.signal.aborted &&
            requestGeneration === generation.current
          ) {
            setState((current) => ({ ...current, loading: false }));
          } else if (!controller.signal.aborted && requestGeneration === generation.current) {
            setState((current) => ({
              ...current,
              loading: false,
              error: safeError(error),
              retryPage: pending
                ? { offset: 0, limit: RUN_INITIAL_PAGE_SIZE }
                : { offset: nextOffset, limit: nextLimit },
            }));
          }
        } finally {
          if (chainController.current === controller) chainController.current = null;
        }
      })();
    };
    retryContinuation.current = startContinuation;

    const retire = coordinator.observe({
      key: refreshOwnerKey,
      cadenceMs: SUMMARY_REFRESH_MS,
      read: (signal) => readAdaptiveRunsPage(
        dataSource,
        filters,
        0,
        RUN_INITIAL_PAGE_SIZE,
        signal,
      ),
      onSuccess: (observation) => {
        if (requestGeneration !== generation.current) return;
        if (!sameFilterEcho(filters, observation)) {
          setState((current) => ({
            ...current,
            loading: false,
            error: "The run map response did not match the current filter scope.",
          }));
          return;
        }
        const items = mergeRuns([], observation.data.items);
        const firstPage: PendingPageChain = {
          items,
          totalCount: observation.data.totalCount,
          coverage: observation.data.coverage,
          page: observation.data.page,
          observations: recordPageObservation([], 0, observation),
        };
        if (observation.data.page.hasMore && observation.data.page.nextOffset !== null) {
          const preserveSuccessfulHistory = hasSuccessfulHistory.current;
          if (items.length > 0) hasSuccessfulHistory.current = true;
          if (!preserveSuccessfulHistory) {
            setState((current) => ({
              ...current,
              ...firstPage,
              loading: true,
              error: null,
              retryPage: null,
            }));
          } else {
            setState((current) => {
              const visibleItems = mergeRuns(current.items, items);
              return {
                ...current,
                items: visibleItems,
                totalCount: Math.max(observation.data.totalCount, visibleItems.length),
                coverage: observation.data.coverage,
                page: observation.data.page,
                observations: recordPageObservation(current.observations, 0, observation),
                loading: true,
                error: null,
                retryPage: null,
              };
            });
          }
          startContinuation(
            observation.data.page.nextOffset,
            observation.data.page.limit,
            preserveSuccessfulHistory ? firstPage : null,
          );
        } else {
          chainController.current?.abort();
          chainController.current = null;
          hasSuccessfulHistory.current = items.length > 0;
          setState((current) => ({
            ...current,
            ...firstPage,
            loading: false,
            error: null,
            retryPage: null,
          }));
        }
      },
      onError: (error) => {
        if (requestGeneration === generation.current) {
          setState((current) => ({
            ...current,
            loading: false,
            error: safeError(error),
            retryPage: { offset: 0, limit: RUN_INITIAL_PAGE_SIZE },
          }));
        }
      },
    });
    return () => {
      retire();
      chainController.current?.abort();
      chainController.current = null;
      retryContinuation.current = null;
    };
  }, [coordinator, dataSource, datasetKey, filters, refreshOwnerKey, scopeKey]);

  useEffect(() => () => chainController.current?.abort(), []);

  const groups = useMemo(() => groupRunMapItems(state.items), [state.items]);
  const option = useMemo(
    () => runMapOption(state.items, filters.timeZone),
    [filters.timeZone, state.items],
  );
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

  const select = (runId: string) => {
    if (runId === "") {
      onSelectRun(null);
      return;
    }
    const run = state.items.find((item) => item.runId === runId);
    if (!run) return;
    onSelectRun(run);
  };

  const chartEvents = useMemo(
    () => ({
      click: (event: unknown) => {
        const runId = runIdFromChartEvent(event);
        if (runId) select(runId);
      },
    }),
    // The item list is replaced on each page response; exact IDs fence selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [state.items, onSelectRun],
  );

  return (
    <section className="panel run-map-panel" aria-labelledby="map-heading">
      <div className="panel-heading">
        <h2 id="map-heading">Sequence and run map</h2>
        <div className="panel-heading-actions">
          <strong aria-live="polite">
            Loaded {state.items.length} of {state.totalCount} started runs
          </strong>
          <InformationControl label="Sequence and run map">
            <p>Every cell is one authoritative run ID; replacement generations are never collapsed.</p>
            <p>The client requests up to 1000 rows, reduces only a typed oversized page, and follows each returned continuation automatically. Distinct pages can be observed at different times and are not presented as one atomic snapshot.</p>
            {state.observations.length ? (
              <ul>
                {state.observations.map((item) => (
                  <li key={item.offset}>
                    Offset {item.offset}: source observed {formatInstant(item.observedAt, filters.timeZone)};
                    received {formatInstant(item.receivedAt, filters.timeZone)} ({formatAge(item.receivedAt)}).
                  </li>
                ))}
              </ul>
            ) : null}
          </InformationControl>
        </div>
      </div>
      {state.coverage ? (
        <p className={`coverage coverage-${state.coverage.dataState}`}>
          Dataset coverage: {state.coverage.dataState}
          {state.coverage.warnings.length ? ` · ${state.coverage.warnings.join(", ")}` : ""}
        </p>
      ) : null}
      {state.observations.length ? (
        <p className="observation-line">
          Latest successful run page · {formatAge(state.observations.at(-1)!.receivedAt)}
        </p>
      ) : null}
      {state.error ? (
        <p className="error-banner" role="alert">
          Run map: {state.error}
          {state.observations.length
            ? ` · showing ${state.items.length} distinct IDs from successful pages`
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
          <label className="run-map-selector">
            Inspect one loaded run
            <select
              aria-label="Select loaded run"
              value={selectedRunId ?? ""}
              onChange={(event) => select(event.target.value)}
            >
              <option value="">No run selected</option>
              {groups.flatMap((group) => group.items.map((run) => (
                <option key={run.runId} value={run.runId}>
                  {group.label} · {run.shortName} · {run.implementerProvider} · {run.runId}
                </option>
              )))}
            </select>
          </label>
        </>
      ) : !state.loading && (state.observations.length > 0 || !state.error) ? (
        <div className="empty-state">No started runs match the applied scope.</div>
      ) : !state.loading ? (
        <div className="empty-state">Run data is unavailable until a successful read.</div>
      ) : null}
      {state.loading ? <p className="loading-line">Loading the complete matching run cohort…</p> : null}
      {state.retryPage ? (
        <button
          type="button"
          disabled={state.loading}
          onClick={() => {
            const retry = state.retryPage;
            if (retry === null) return;
            if (retry.offset === 0) coordinator.refresh(refreshOwnerKey);
            else retryContinuation.current?.(retry.offset, retry.limit);
          }}
        >
          Retry run loading
        </button>
      ) : null}
    </section>
  );
}
