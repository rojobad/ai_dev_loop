import type { CustomSeriesRenderItem, EChartsCoreOption } from "echarts";
import React, { useEffect, useMemo, useRef, useState } from "react";

import { DashboardChart } from "./DashboardChart";
import { escapeHtml } from "./charts";
import type {
  AttemptTimelineItem,
  DashboardDataSource,
  DashboardPage,
  DashboardRunItem,
  RunInspectData,
  SourceObservation,
} from "./types";
import { DashboardDataSourceError } from "./types";

const TIMELINE_PAGE_SIZE = 25;
const SELECTION_SATURATION_RETRY_DELAYS_MS = [100, 200, 400, 800, 1600] as const;

export type AttemptRole =
  | "Cursor implementer"
  | "Codex implementer"
  | "Codex reviewer"
  | "Non-execution / unclassified";

export type TimelineShape = "interval" | "launch-marker" | "created-marker";

export interface TimelineVisualRow {
  attempt: AttemptTimelineItem;
  role: AttemptRole;
  label: string;
  startMs: number;
  endMs: number | null;
  shape: TimelineShape;
}

interface TimelineState {
  items: AttemptTimelineItem[];
  page?: DashboardPage;
  observations: string[];
  loading: boolean;
  error: string | null;
}

export interface AttemptTimelineProps {
  dataSource: DashboardDataSource;
  selectedRun: DashboardRunItem | null;
  theme: "light" | "dark";
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The selected run read failed.";
}

function cancellableDelay(delayMs: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason ?? new DOMException("The request was aborted.", "AbortError"));
      return;
    }
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delayMs);
    const abort = () => {
      window.clearTimeout(timer);
      reject(signal.reason ?? new DOMException("The request was aborted.", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function readCurrentSelection<T>(
  read: () => Promise<T>,
  signal: AbortSignal,
): Promise<T> {
  for (let retry = 0; ; retry += 1) {
    try {
      return await read();
    } catch (error) {
      if (signal.aborted) throw error;
      const saturated =
        error instanceof DashboardDataSourceError && error.code === "SERVICE_UNAVAILABLE";
      if (!saturated) throw error;
      if (retry >= SELECTION_SATURATION_RETRY_DELAYS_MS.length) {
        throw new DashboardDataSourceError(
          "SERVICE_UNAVAILABLE",
          "The local dashboard remained saturated after bounded selected-run retries.",
        );
      }
      await cancellableDelay(SELECTION_SATURATION_RETRY_DELAYS_MS[retry], signal);
    }
  }
}

export function attemptRole(attempt: AttemptTimelineItem): AttemptRole {
  if (attempt.component === "cursor" && attempt.effectKind === "cursor.run_turn") {
    return "Cursor implementer";
  }
  if (
    attempt.component === "codex_implementer" &&
    (attempt.effectKind === "codex.implementer_bootstrap" ||
      attempt.effectKind === "codex.implementer_resume")
  ) {
    return "Codex implementer";
  }
  if (
    attempt.component === "codex" &&
    (attempt.effectKind === "codex.bootstrap_review" ||
      attempt.effectKind === "codex.resume_review")
  ) {
    return "Codex reviewer";
  }
  return "Non-execution / unclassified";
}

export function buildTimelineRows(items: AttemptTimelineItem[]): TimelineVisualRow[] {
  return items.map((attempt) => {
    const launch = attempt.launchRequestedAt === null ? null : Date.parse(attempt.launchRequestedAt);
    const completed = attempt.completedAt === null ? null : Date.parse(attempt.completedAt);
    const created = Date.parse(attempt.createdAt);
    const hasValidInterval = launch !== null && completed !== null && completed >= launch;
    const role = attemptRole(attempt);
    return {
      attempt,
      role,
      label: `${role} · iteration ${attempt.iteration} · attempt ${attempt.phaseAttempt}`,
      startMs: launch ?? created,
      endMs: hasValidInterval ? completed : null,
      shape: hasValidInterval ? "interval" : launch === null ? "created-marker" : "launch-marker",
    };
  });
}

function colorForRole(role: AttemptRole): string {
  if (role === "Cursor implementer") return "#2f76d2";
  if (role === "Codex implementer") return "#8054ae";
  if (role === "Codex reviewer") return "#16816d";
  return "#727984";
}

const renderAttempt: CustomSeriesRenderItem = (_params, api) => {
  const start = api.coord([Number(api.value(0)), Number(api.value(2))]);
  const end = api.coord([Number(api.value(1)), Number(api.value(2))]);
  const shapeCode = Number(api.value(3));
  if (shapeCode === 0) {
    return {
      type: "rect",
      shape: {
        x: start[0],
        y: start[1] - 7,
        width: Math.max(2, end[0] - start[0]),
        height: 14,
        r: 3,
      },
      style: api.style(),
    };
  }
  return {
    type: shapeCode === 1 ? "circle" : "polygon",
    shape:
      shapeCode === 1
        ? { cx: start[0], cy: start[1], r: 7 }
        : {
            points: [
              [start[0], start[1] - 8],
              [start[0] + 8, start[1]],
              [start[0], start[1] + 8],
              [start[0] - 8, start[1]],
            ],
          },
    style: api.style(),
  };
};

export function attemptTimelineOption(items: AttemptTimelineItem[]): EChartsCoreOption {
  const rows = buildTimelineRows(items);
  return {
    animation: false,
    grid: { left: 18, right: 22, top: 18, bottom: 68, containLabel: true },
    tooltip: {
      renderMode: "html",
      formatter: (value: unknown) => {
        const event = typeof value === "object" && value !== null
          ? (value as Record<string, unknown>)
          : {};
        const data = typeof event.data === "object" && event.data !== null
          ? (event.data as Record<string, unknown>)
          : {};
        const line = (key: string, fallback: string) =>
          escapeHtml(typeof data[key] === "string" ? (data[key] as string) : fallback);
        const endLabel = data.shape === "interval" ? line("completedAt", "unknown") : "No observed duration";
        return `<strong>${line("attemptId", "Attempt")}</strong><br>${line("role", "Unclassified")}<br>${line("component", "unknown")} · ${line("effectKind", "no effect")}<br>${line("status", "unknown")}<br>${line("startAt", "unknown")} → ${endLabel}`;
      },
    },
    xAxis: { type: "time", name: "Observed scheduler timestamps" },
    yAxis: {
      type: "category",
      inverse: true,
      data: rows.map((row) => row.label),
      axisLabel: { width: 210, overflow: "truncate" },
    },
    series: [
      {
        type: "custom",
        renderItem: renderAttempt,
        encode: { x: [0, 1], y: 2 },
        data: rows.map((row, index) => ({
          value: [row.startMs, row.endMs ?? row.startMs, index, row.shape === "interval" ? 0 : row.shape === "launch-marker" ? 1 : 2],
          attemptId: row.attempt.attemptId,
          role: row.role,
          component: row.attempt.component,
          effectKind: row.attempt.effectKind,
          status: row.attempt.status,
          startAt: row.attempt.launchRequestedAt ?? row.attempt.createdAt,
          completedAt: row.attempt.completedAt,
          shape: row.shape,
          itemStyle: { color: colorForRole(row.role) },
        })),
      },
    ],
  };
}

export function attemptIdFromChartEvent(value: unknown): string | null {
  if (typeof value !== "object" || value === null) return null;
  const event = value as Record<string, unknown>;
  if (typeof event.data !== "object" || event.data === null) return null;
  const attemptId = (event.data as Record<string, unknown>).attemptId;
  return typeof attemptId === "string" && attemptId.length > 0 ? attemptId : null;
}

function mergeAttempts(
  current: AttemptTimelineItem[],
  incoming: AttemptTimelineItem[],
): AttemptTimelineItem[] {
  const byId = new Map(current.map((item) => [item.attemptId, item]));
  for (const item of incoming) byId.set(item.attemptId, item);
  return [...byId.values()];
}

function formatOptional(value: string | null): string {
  return value ?? "not recorded";
}

export function AttemptTimeline({
  dataSource,
  selectedRun,
  theme,
}: AttemptTimelineProps): React.JSX.Element {
  const [inspect, setInspect] = useState<{
    loading: boolean;
    error: string | null;
    observation?: SourceObservation<RunInspectData>;
  }>({ loading: false, error: null });
  const [timeline, setTimeline] = useState<TimelineState>({
    items: [],
    observations: [],
    loading: false,
    error: null,
  });
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);
  const generation = useRef(0);
  const inspectController = useRef<AbortController | null>(null);
  const timelineController = useRef<AbortController | null>(null);
  const attemptButtons = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    const requestGeneration = ++generation.current;
    inspectController.current?.abort();
    timelineController.current?.abort();
    setSelectedAttemptId(null);
    setInspect({ loading: selectedRun !== null, error: null });
    setTimeline({
      items: [],
      observations: [],
      loading: selectedRun !== null,
      error: null,
    });
    if (selectedRun === null) return;
    const runId = selectedRun.runId;
    const metadataController = new AbortController();
    const pageController = new AbortController();
    inspectController.current = metadataController;
    timelineController.current = pageController;
    void readCurrentSelection(
      () => dataSource.runInspect(runId, metadataController.signal),
      metadataController.signal,
    )
      .then((observation) => {
        if (metadataController.signal.aborted || requestGeneration !== generation.current) return;
        if (observation.data.runId !== runId) {
          throw new Error("The run metadata response did not match the selected run ID.");
        }
        setInspect({ loading: false, error: null, observation });
      })
      .catch((error) => {
        if (!metadataController.signal.aborted && requestGeneration === generation.current) {
          setInspect({ loading: false, error: safeError(error) });
        }
      });
    void readCurrentSelection(
      () =>
        dataSource.runTimeline(
          runId,
          { offset: 0, limit: TIMELINE_PAGE_SIZE },
          pageController.signal,
        ),
      pageController.signal,
    )
      .then((observation) => {
        if (pageController.signal.aborted || requestGeneration !== generation.current) return;
        if (observation.data.runId !== runId) {
          throw new Error("The timeline response did not match the selected run ID.");
        }
        setTimeline({
          items: mergeAttempts([], observation.data.items),
          page: observation.data.page,
          observations: [observation.observedAt],
          loading: false,
          error: null,
        });
      })
      .catch((error) => {
        if (!pageController.signal.aborted && requestGeneration === generation.current) {
          setTimeline((current) => ({ ...current, loading: false, error: safeError(error) }));
        }
      });
    return () => {
      metadataController.abort();
      pageController.abort();
    };
  }, [dataSource, selectedRun]);

  async function loadMore(offset: number) {
    if (selectedRun === null) return;
    const requestGeneration = generation.current;
    timelineController.current?.abort();
    const controller = new AbortController();
    timelineController.current = controller;
    setTimeline((current) => ({ ...current, loading: true, error: null }));
    try {
      const observation = await readCurrentSelection(
        () =>
          dataSource.runTimeline(
            selectedRun.runId,
            { offset, limit: TIMELINE_PAGE_SIZE },
            controller.signal,
          ),
        controller.signal,
      );
      if (controller.signal.aborted || requestGeneration !== generation.current) return;
      if (observation.data.runId !== selectedRun.runId) {
        throw new Error("The timeline response did not match the selected run ID.");
      }
      setTimeline((current) => ({
        items: mergeAttempts(current.items, observation.data.items),
        page: observation.data.page,
        observations: [...current.observations, observation.observedAt],
        loading: false,
        error: null,
      }));
    } catch (error) {
      if (!controller.signal.aborted && requestGeneration === generation.current) {
        setTimeline((current) => ({ ...current, loading: false, error: safeError(error) }));
      }
    }
  }

  const rows = useMemo(() => buildTimelineRows(timeline.items), [timeline.items]);
  const option = useMemo(() => attemptTimelineOption(timeline.items), [timeline.items]);

  const selectAttempt = (attemptId: string, focusCompanion: boolean) => {
    if (!timeline.items.some((item) => item.attemptId === attemptId)) return;
    setSelectedAttemptId(attemptId);
    if (focusCompanion) {
      requestAnimationFrame(() => {
        const button = attemptButtons.current.get(attemptId);
        button?.focus({ preventScroll: true });
        button?.scrollIntoView({ block: "nearest" });
      });
    }
  };

  const chartEvents = useMemo(
    () => ({
      click: (event: unknown) => {
        const attemptId = attemptIdFromChartEvent(event);
        if (attemptId) selectAttempt(attemptId, true);
      },
    }),
    // Exact attempt IDs are regenerated only from the current selected run pages.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [timeline.items],
  );

  if (selectedRun === null) {
    return (
      <section className="panel attempt-timeline-panel" aria-labelledby="timeline-heading">
        <h2 id="timeline-heading">Attempt timeline</h2>
        <div className="empty-state">Select an exact run cell or companion row to inspect it.</div>
      </section>
    );
  }

  const metadata = inspect.observation?.data;
  const selectedAttempt = timeline.items.find((item) => item.attemptId === selectedAttemptId);
  return (
    <section className="panel attempt-timeline-panel" aria-labelledby="timeline-heading">
      <div className="panel-heading">
        <div>
          <h2 id="timeline-heading">Attempt timeline</h2>
          <p>Selected run: <strong>{selectedRun.runId}</strong></p>
        </div>
        <span>{timeline.items.length} loaded / {metadata?.attemptCount ?? "?"} recorded attempts</span>
      </div>
      {inspect.error ? <p className="error-banner" role="alert">Run metadata: {inspect.error}</p> : null}
      {metadata ? (
        <dl className="run-metadata">
          <div><dt>Project</dt><dd>{metadata.projectName}</dd></div>
          <div><dt>Raw state</dt><dd>{metadata.state}</dd></div>
          <div><dt>Implementer</dt><dd>{metadata.implementerProvider}</dd></div>
          <div><dt>Review budget</dt><dd>{metadata.reviewBudget.completed} / {metadata.reviewBudget.max}</dd></div>
          <div><dt>Pending Codex role / turn</dt><dd>{formatOptional(metadata.codexPendingRole)} / {formatOptional(metadata.codexPendingTurn)}</dd></div>
          <div><dt>Capacity reason</dt><dd>{formatOptional(metadata.codexCapacityReason)}</dd></div>
          <div><dt>Implementer retry wait</dt><dd>{formatOptional(metadata.implementerRetryWaitUntil)}</dd></div>
          <div><dt>Timeout retries</dt><dd>{metadata.implementerTimeoutAutomaticRetries ?? "not recorded"} / {metadata.implementerTimeoutRetryLimit ?? "not recorded"}</dd></div>
          <div><dt>Recorded next action</dt><dd>{metadata.safeNextAction.kind}</dd></div>
        </dl>
      ) : inspect.loading ? <p className="loading-line">Reading selected run metadata…</p> : null}
      {selectedRun.sequenceBinding ? (
        <p className="lineage-line">
          {selectedRun.sequenceBinding.sequenceName} · phase {selectedRun.sequenceBinding.ordinal} {selectedRun.sequenceBinding.phaseName} · generation {selectedRun.sequenceBinding.generation} · {selectedRun.sequenceBinding.attemptKind}
          {selectedRun.sequenceBinding.sourceRunId ? ` · replaces ${selectedRun.sequenceBinding.sourceRunId}` : ""}
        </p>
      ) : <p className="lineage-line">Standalone run</p>}
      {timeline.error ? <p className="error-banner" role="alert">Timeline: {timeline.error}</p> : null}
      {timeline.observations.length ? (
        <p className="observation-line">
          Timeline page observations: {timeline.observations.join(" · ")} (page exhaustion does not complete an attempt)
        </p>
      ) : null}
      {timeline.items.length ? (
        <>
          <DashboardChart
            ariaLabel={`Gantt timeline for ${selectedRun.runId}`}
            height={Math.max(260, timeline.items.length * 40)}
            onEvents={chartEvents}
            option={option}
            ownershipKey={selectedRun.runId}
            theme={theme}
          />
          <div className="attempt-rows" aria-label="Keyboard-selectable attempt timeline rows">
            {rows.map((row) => (
              <button
                type="button"
                key={row.attempt.attemptId}
                ref={(node) => {
                  if (node) attemptButtons.current.set(row.attempt.attemptId, node);
                  else attemptButtons.current.delete(row.attempt.attemptId);
                }}
                aria-pressed={selectedAttemptId === row.attempt.attemptId}
                onClick={() => selectAttempt(row.attempt.attemptId, false)}
              >
                <span className="run-row-title">{row.attempt.attemptId}</span>
                <span>{row.role} · {row.attempt.status}</span>
                <span>{row.attempt.component} · {row.attempt.effectKind ?? "no recorded effect"}</span>
                <span>
                  {row.shape === "interval"
                    ? `${row.attempt.launchRequestedAt} → ${row.attempt.completedAt}`
                    : row.shape === "launch-marker"
                      ? `Launched ${row.attempt.launchRequestedAt}; no observed end`
                      : `Created ${row.attempt.createdAt}; not launched`}
                </span>
              </button>
            ))}
          </div>
          {selectedAttempt ? (
            <p className="selected-attempt" aria-live="polite">
              Selected attempt {selectedAttempt.attemptId}; metadata selection is ready. No content was loaded.
            </p>
          ) : null}
        </>
      ) : !timeline.loading ? <div className="empty-state">No recorded attempts for this run.</div> : null}
      {timeline.loading ? <p className="loading-line">Reading authoritative attempt page…</p> : null}
      {timeline.page?.hasMore && timeline.page.nextOffset !== null ? (
        <button
          type="button"
          disabled={timeline.loading}
          onClick={() => void loadMore(timeline.page!.nextOffset!)}
        >
          Load more attempts
        </button>
      ) : timeline.page ? (
        <p className="field-help">No more timeline rows were observed; active/unknown attempts remain explicitly unresolved.</p>
      ) : null}
    </section>
  );
}
