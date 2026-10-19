import type { EChartsCoreOption } from "echarts/core";
import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { DashboardChart } from "./DashboardChart";
import { InformationControl } from "./InformationControl";
import {
  SUMMARY_REFRESH_MS,
  RefreshCoordinator,
  canonicalReadKey,
} from "./refreshCoordinator";
import {
  formatAge,
  formatCalendarDay,
  formatDuration,
  formatInstant,
} from "./time";
import type {
  DashboardActivityData,
  DashboardActivityMetric,
  DashboardDataSource,
  DashboardFilters,
  SourceObservation,
} from "./types";

interface ActivityState {
  loading: boolean;
  error: string | null;
  observation?: SourceObservation<DashboardActivityData>;
}

export interface ActivityCalendarProps {
  coordinator: RefreshCoordinator;
  dataSource: DashboardDataSource;
  datasetKey: string;
  filters: DashboardFilters;
  theme: "light" | "dark";
}

const METRIC_LABELS: Record<DashboardActivityMetric, string> = {
  submissions: "Submissions",
  "review-rounds": "Review rounds",
  "process-hours": "Process hours",
};

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The activity read failed.";
}

function calendarDate(instant: string, timeZone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(instant));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

export function calendarRange(data: DashboardActivityData): [string, string] {
  const finalIncluded = new Date(Date.parse(data.filters.toUtc) - 1).toISOString();
  return [
    calendarDate(data.filters.fromUtc, data.filters.timeZone),
    calendarDate(finalIncluded, data.filters.timeZone),
  ];
}

export function activityDisplayValue(
  metric: DashboardActivityMetric,
  value: number,
): number {
  return metric === "process-hours" ? value / 3600 : value;
}

function formatValue(metric: DashboardActivityMetric, value: number): string {
  if (metric !== "process-hours") return `${value}`;
  return formatDuration(value);
}

export function activityCalendarOption(data: DashboardActivityData): EChartsCoreOption {
  const displayed = data.days.map((day) => [
    day.date,
    activityDisplayValue(data.metric, day.value),
  ]);
  const max = Math.max(1, ...displayed.map((item) => Number(item[1])));
  return {
    animation: false,
    tooltip: {
      position: "top",
      renderMode: "html",
      formatter: (eventValue: unknown) => {
        const item = typeof eventValue === "object" && eventValue !== null
          ? (eventValue as Record<string, unknown>)
          : {};
        const raw = Array.isArray(item.value) ? item.value : [];
        const date = typeof raw[0] === "string" ? raw[0] : "Unknown date";
        const day = data.days.find((candidate) => candidate.date === date);
        const value = day?.value ?? 0;
        const amount = formatValue(data.metric, value);
        return `<strong>${formatCalendarDay(date)}</strong><br>${amount}${
          data.metric === "process-hours" ? "" : " events"
        }`;
      },
    },
    visualMap: {
      min: 0,
      max,
      calculable: false,
      orient: "horizontal",
      left: "center",
      bottom: 0,
      inRange: { color: ["#e8eef7", "#7aa8e8", "#2f76d2"] },
    },
    calendar: {
      range: calendarRange(data),
      top: 34,
      left: 48,
      right: 22,
      bottom: 58,
      cellSize: ["auto", 18],
      splitLine: { show: true },
      yearLabel: { show: true, margin: 26 },
      monthLabel: { nameMap: "EN" },
      dayLabel: { firstDay: 1, nameMap: "EN" },
    },
    series: [{ type: "heatmap", coordinateSystem: "calendar", data: displayed }],
  };
}

function sameFilter(data: DashboardActivityData, filters: DashboardFilters): boolean {
  return (
    Date.parse(data.filters.fromUtc) === Date.parse(filters.fromUtc) &&
    Date.parse(data.filters.toUtc) === Date.parse(filters.toUtc) &&
    data.filters.timeZone === filters.timeZone &&
    data.filters.projectName === (filters.projectName ?? null)
  );
}

export function ActivityCalendar({
  coordinator,
  dataSource,
  datasetKey,
  filters,
  theme,
}: ActivityCalendarProps): React.JSX.Element {
  const [metric, setMetric] = useState<DashboardActivityMetric>("submissions");
  const [state, setState] = useState<ActivityState>({ loading: true, error: null });
  const generation = useRef(0);
  const results = useRef<HTMLDivElement>(null);
  const reservedHeight = useRef(260);
  const key = useMemo(
    () =>
      canonicalReadKey("dashboard.activity", {
        fromUtc: filters.fromUtc,
        toUtc: filters.toUtc,
        timeZone: filters.timeZone,
        projectName: filters.projectName ?? null,
        metric,
        datasetKey,
      }),
    [datasetKey, filters, metric],
  );

  // Capture the current geometry before the next metric/scope retires its
  // observation. Keeping this space prevents the browser clamping scrollY
  // when a calendar at the bottom of the document temporarily disappears.
  useLayoutEffect(() => {
    if (results.current) {
      reservedHeight.current = results.current.getBoundingClientRect().height;
    }
  }, [key]);

  useEffect(() => {
    const ownedGeneration = ++generation.current;
    setState((current) => ({
      loading: true,
      error: null,
      observation:
        current.observation?.data.metric === metric &&
        sameFilter(current.observation.data, filters)
          ? current.observation
          : undefined,
    }));
    return coordinator.observe({
      key,
      cadenceMs: SUMMARY_REFRESH_MS,
      read: (signal) => dataSource.activity(filters, metric, signal),
      onSuccess: (observation) => {
        if (ownedGeneration !== generation.current) return;
        if (observation.data.metric !== metric || !sameFilter(observation.data, filters)) {
          setState((current) => ({
            loading: false,
            error: "The activity response did not match the selected metric and applied scope.",
            observation: current.observation,
          }));
          return;
        }
        setState({ loading: false, error: null, observation });
      },
      onError: (error) => {
        if (ownedGeneration !== generation.current) return;
        setState((current) => ({
          loading: false,
          error: safeError(error),
          observation: current.observation,
        }));
      },
    });
  }, [coordinator, dataSource, filters, key, metric]);

  const data = state.observation?.data;
  const option = useMemo(() => (data ? activityCalendarOption(data) : null), [data]);
  const partial = data?.coverage.dataState === "partial";

  return (
    <section className="panel activity-panel" aria-labelledby="activity-heading">
      <div className="panel-heading activity-heading">
        <h2 id="activity-heading">Activity calendar</h2>
        <div className="panel-heading-actions">
          <label>
            Metric
            <select
              aria-label="Activity metric"
              value={metric}
              onChange={(event) => setMetric(event.target.value as DashboardActivityMetric)}
            >
              {(Object.keys(METRIC_LABELS) as DashboardActivityMetric[]).map((value) => (
                <option key={value} value={value}>{METRIC_LABELS[value]}</option>
              ))}
            </select>
          </label>
          <InformationControl label="Activity calendar">
            <p>Activity is dated by event in {filters.timeZone}; it differs from the overview&apos;s first-execution cohort.</p>
            <p>Submissions use submission dates. Review rounds count the first reviewer launch per run and iteration; implementer attempts never become review rounds.</p>
            <p>Process hours include resolved implementation and review intervals, including resolved failures or cancellations, and sum parallel process time before display conversion.</p>
            {state.observation ? (
              <p>
                Source observed {formatInstant(state.observation.observedAt, filters.timeZone)};
                received {formatInstant(state.observation.receivedAt, filters.timeZone)}.
              </p>
            ) : null}
          </InformationControl>
        </div>
      </div>
      {state.error ? (
        <p className="error-banner" role="alert">
          Activity refresh: {state.error}
          {state.observation
            ? ` · showing the last successful observation (${formatAge(state.observation.receivedAt)})`
            : ""}
        </p>
      ) : null}
      <div ref={results} className="activity-results" aria-busy={state.loading}>
        {state.observation && data ? (
          <>
            <p className="observation-line">
              Last successful activity read · {formatAge(state.observation.receivedAt)}
            </p>
            <p className={`coverage coverage-${data.coverage.dataState}`}>
              Coverage: {data.coverage.dataState}. {partial
                ? "Values are recorded lower bounds; absent days are unknown."
                : "Returned zero days are explicit."}
              {data.coverage.warnings.length ? ` Warnings: ${data.coverage.warnings.join(", ")}.` : ""}
              {` Exclusions — unstarted runs ${data.coverage.unstartedRuns}, missing launches ${data.coverage.missingLaunchTimes}, unknown execution kinds ${data.coverage.unknownExecutionKinds}, missing durations ${data.coverage.missingDurationAttempts}.`}
            </p>
            <p className="activity-total">
              {METRIC_LABELS[data.metric]} total: <strong>{formatValue(data.metric, data.total)}</strong>
            </p>
            {option && data.days.length ? (
              <DashboardChart
                ariaLabel={`${METRIC_LABELS[data.metric]} activity calendar in ${data.filters.timeZone}`}
                height={260}
                option={option}
                ownershipKey={key}
                theme={theme}
              />
            ) : (
              <div className="empty-state">
                {partial
                  ? "No recorded contributions were returned; absent days remain unknown."
                  : "No activity was recorded in the applied range."}
              </div>
            )}
            <details className="activity-days-disclosure">
              <summary>Show daily values</summary>
              <ol className="activity-days" aria-label={`${METRIC_LABELS[data.metric]} daily values`}>
                {data.days.map((day) => (
                  <li key={day.date}>
                    <time dateTime={day.date}>{formatCalendarDay(day.date)}</time>
                    <span>{formatValue(data.metric, day.value)}</span>
                  </li>
                ))}
              </ol>
            </details>
          </>
        ) : (
          <div className="activity-loading" style={{ minHeight: reservedHeight.current }}>
            <p className="loading-line" role="status">
              {state.loading ? "Reading activity…" : "Activity is unavailable."}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
