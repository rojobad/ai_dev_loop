import type { CustomSeriesRenderItem, EChartsCoreOption } from "echarts";
import React, { useEffect, useMemo, useRef, useState } from "react";

import { AttemptModal } from "./AttemptModal";
import { DashboardChart } from "./DashboardChart";
import { InformationControl } from "./InformationControl";
import { escapeHtml } from "./charts";
import { CoordinatedDashboardDataSource } from "./coordinated-data-source";
import {
  SELECTED_RUN_REFRESH_MS,
  RefreshCoordinator,
  canonicalReadKey,
} from "./refreshCoordinator";
import {
  formatAge,
  formatAxisInstant,
  formatDuration,
  formatInstant,
} from "./time";
import type {
  AttemptTimelineItem,
  DashboardDataSource,
  DashboardPage,
  DashboardRunItem,
  RunInspectData,
  SourceObservation,
} from "./types";

const TIMELINE_PAGE_SIZE = 25;

export type AttemptRole =
  | "Cursor implementer"
  | "Codex implementer"
  | "Codex reviewer"
  | "Unclassified";

export type TimelineShape = "interval" | "launch-marker" | "created-marker";

export interface TimelineVisualRow {
  attempt: AttemptTimelineItem;
  ordinal: number;
  role: AttemptRole;
  caption: string;
  startMs: number;
  endMs: number | null;
  shape: TimelineShape;
}

interface TimelineState {
  items: AttemptTimelineItem[];
  page?: DashboardPage;
  observations: TimelinePageObservation[];
  loading: boolean;
  error: string | null;
  firstPageIds: string[];
}

interface TimelinePageObservation {
  offset: number;
  observedAt: string;
  receivedAt: string;
}

export interface AttemptTimelineProps {
  dataSource: DashboardDataSource;
  coordinator: RefreshCoordinator;
  selectedRun: DashboardRunItem | null;
  theme: "light" | "dark";
  timeZone?: string;
}

function safeError(error: unknown): string {
  return error instanceof Error ? error.message : "The selected run read failed.";
}

function recordPageObservation(
  current: TimelinePageObservation[],
  offset: number,
  observation: SourceObservation<unknown>,
): TimelinePageObservation[] {
  return [
    ...current.filter((item) => item.offset !== offset),
    { offset, observedAt: observation.observedAt, receivedAt: observation.receivedAt },
  ].sort((left, right) => left.offset - right.offset);
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
  return "Unclassified";
}

export function attemptCaption(attempt: AttemptTimelineItem, ordinal: number): string {
  const role = attemptRole(attempt);
  const captionRole = role === "Codex reviewer"
    ? "Reviewer"
    : role === "Unclassified"
      ? "Unclassified"
      : "Implementer";
  return `${ordinal} - Iteration ${attempt.iteration} - ${captionRole}`;
}

export function buildTimelineRows(items: AttemptTimelineItem[]): TimelineVisualRow[] {
  return items.map((attempt, index) => {
    const launch = attempt.launchRequestedAt === null ? null : Date.parse(attempt.launchRequestedAt);
    const completed = attempt.completedAt === null ? null : Date.parse(attempt.completedAt);
    const created = Date.parse(attempt.createdAt);
    const hasValidInterval = launch !== null && completed !== null && completed >= launch;
    const role = attemptRole(attempt);
    const ordinal = index + 1;
    return {
      attempt,
      ordinal,
      role,
      caption: attemptCaption(attempt, ordinal),
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

export function attemptTimelineOption(
  items: AttemptTimelineItem[],
  timeZone = "UTC",
): EChartsCoreOption {
  const rows = buildTimelineRows(items);
  return {
    animation: false,
    grid: { left: 18, right: 22, top: 18, bottom: 24, containLabel: true },
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
        const endLabel = data.shape === "interval"
          ? line("completedAt", "Unavailable")
          : "No observed duration";
        return `<strong>${line("caption", "Attempt")}</strong><br>${line("role", "Unclassified")}<br>${line("component", "unknown")} · ${line("effectKind", "no effect")}<br>${line("status", "unknown")}<br>${line("startAt", "Unavailable")} → ${endLabel}<br>Duration: ${line("duration", "Unavailable")}`;
      },
    },
    xAxis: {
      type: "time",
      splitNumber: 4,
      minInterval: 60_000,
      axisLabel: {
        formatter: (value: number) => formatAxisInstant(value, timeZone),
        rotate: 90,
        hideOverlap: true,
        margin: 12,
      },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: rows.map((row) => row.caption),
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
          caption: row.caption,
          role: row.role,
          component: row.attempt.component,
          effectKind: row.attempt.effectKind,
          status: row.attempt.status,
          startAt: formatInstant(
            row.attempt.launchRequestedAt ?? row.attempt.createdAt,
            timeZone,
          ),
          completedAt: row.attempt.completedAt === null
            ? null
            : formatInstant(row.attempt.completedAt, timeZone),
          duration: formatDuration(row.attempt.observedDurationSeconds),
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
  coordinator,
  selectedRun,
  theme,
  timeZone = "UTC",
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
    firstPageIds: [],
  });
  const [selectedAttemptId, setSelectedAttemptId] = useState<string | null>(null);
  const generation = useRef(0);
  const pageController = useRef<AbortController | null>(null);
  const attemptButtons = useRef(new Map<string, HTMLButtonElement>());
  const attemptDisclosure = useRef<HTMLDetailsElement>(null);
  const attemptSummary = useRef<HTMLElement>(null);

  useEffect(() => {
    const requestGeneration = ++generation.current;
    pageController.current?.abort();
    setSelectedAttemptId(null);
    setInspect({ loading: selectedRun !== null, error: null });
    setTimeline({
      items: [],
      observations: [],
      loading: selectedRun !== null,
      error: null,
      firstPageIds: [],
    });
    if (selectedRun === null) return;
    const runId = selectedRun.runId;
    const retireInspect = coordinator.observe({
      key: canonicalReadKey("run.inspect", { runId }),
      cadenceMs: SELECTED_RUN_REFRESH_MS,
      read: (signal) => dataSource.runInspect(runId, signal),
      onSuccess: (observation) => {
        if (requestGeneration !== generation.current) return;
        if (observation.data.runId !== runId) {
          setInspect({
            loading: false,
            error: "The run metadata response did not match the selected run ID.",
          });
          return;
        }
        setInspect({ loading: false, error: null, observation });
      },
      onError: (error) => {
        if (requestGeneration === generation.current) {
          setInspect((current) => ({
            loading: false,
            error: safeError(error),
            observation: current.observation,
          }));
        }
      },
    });
    const retireTimeline = coordinator.observe({
      key: canonicalReadKey("run.timeline", { runId, offset: 0, limit: TIMELINE_PAGE_SIZE }),
      cadenceMs: SELECTED_RUN_REFRESH_MS,
      read: (signal) =>
        dataSource.runTimeline(runId, { offset: 0, limit: TIMELINE_PAGE_SIZE }, signal),
      onSuccess: (observation) => {
        if (requestGeneration !== generation.current) return;
        if (observation.data.runId !== runId) {
          setTimeline((current) => ({
            ...current,
            loading: false,
            error: "The timeline response did not match the selected run ID.",
          }));
          return;
        }
        setTimeline((current) => {
          const previousFirstPage = new Set(current.firstPageIds);
          const retainedHistoricPages = current.items.filter(
            (item) => !previousFirstPage.has(item.attemptId),
          );
          return {
            items: mergeAttempts(observation.data.items, retainedHistoricPages),
            page:
              current.page && current.page.offset > 0
                ? current.page
                : observation.data.page,
            observations: recordPageObservation(current.observations, 0, observation),
            loading: false,
            error: null,
            firstPageIds: observation.data.items.map((item) => item.attemptId),
          };
        });
      },
      onError: (error) => {
        if (requestGeneration === generation.current) {
          setTimeline((current) => ({ ...current, loading: false, error: safeError(error) }));
        }
      },
    });
    return () => {
      retireInspect();
      retireTimeline();
    };
  }, [coordinator, dataSource, selectedRun]);

  async function loadMore(offset: number) {
    if (selectedRun === null) return;
    const requestGeneration = generation.current;
    pageController.current?.abort();
    const controller = new AbortController();
    pageController.current = controller;
    setTimeline((current) => ({ ...current, loading: true, error: null }));
    try {
      const observation = await coordinator.request(
        canonicalReadKey("run.timeline.page", {
          runId: selectedRun.runId,
          offset,
          limit: TIMELINE_PAGE_SIZE,
        }),
        (signal) =>
          dataSource.runTimeline(
            selectedRun.runId,
            { offset, limit: TIMELINE_PAGE_SIZE },
            signal,
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
        observations: recordPageObservation(current.observations, offset, observation),
        loading: false,
        error: null,
        firstPageIds: current.firstPageIds,
      }));
    } catch (error) {
      if (!controller.signal.aborted && requestGeneration === generation.current) {
        setTimeline((current) => ({ ...current, loading: false, error: safeError(error) }));
      }
    }
  }

  useEffect(() => () => pageController.current?.abort(), []);

  useEffect(() => {
    if (
      selectedAttemptId !== null &&
      !timeline.items.some((item) => item.attemptId === selectedAttemptId)
    ) {
      setSelectedAttemptId(null);
    }
  }, [selectedAttemptId, timeline.items]);

  const rows = useMemo(() => buildTimelineRows(timeline.items), [timeline.items]);
  const option = useMemo(
    () => attemptTimelineOption(timeline.items, timeZone),
    [timeline.items, timeZone],
  );
  const contentDataSource = useMemo(
    () => new CoordinatedDashboardDataSource(dataSource, coordinator),
    [coordinator, dataSource],
  );

  const selectAttempt = (attemptId: string) => {
    if (!timeline.items.some((item) => item.attemptId === attemptId)) return;
    setSelectedAttemptId(attemptId);
  };

  const closeAttempt = () => {
    const attemptId = selectedAttemptId;
    setSelectedAttemptId(null);
    if (attemptId !== null) {
      requestAnimationFrame(() => {
        const target = attemptDisclosure.current?.open
          ? attemptButtons.current.get(attemptId)
          : attemptSummary.current;
        target?.focus({ preventScroll: true });
      });
    }
  };

  const chartEvents = useMemo(
    () => ({
      click: (event: unknown) => {
        const attemptId = attemptIdFromChartEvent(event);
        if (attemptId) selectAttempt(attemptId);
      },
    }),
    // Exact attempt IDs are regenerated only from the current selected run pages.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [timeline.items],
  );

  if (selectedRun === null) {
    return (
      <section className="panel attempt-timeline-panel" aria-labelledby="timeline-heading">
        <div className="panel-heading">
          <h2 id="timeline-heading">Attempt timeline</h2>
          <InformationControl label="Attempt timeline">
            <p>Select an exact loaded run to read its metadata and authoritative attempt ordering.</p>
            <p>Attempt ordinals remain tied to that ordering. Content is never preloaded and still requires a deliberate control.</p>
          </InformationControl>
        </div>
        <div className="empty-state">Select an exact run cell or the loaded-run control to inspect it.</div>
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
          <p className="selected-run-name" aria-label="Selected run" data-run-id={selectedRun.runId}>
            <strong>{selectedRun.shortName}</strong>
          </p>
        </div>
        <div className="panel-heading-actions">
          <span className="panel-load-count">{timeline.items.length} loaded / {metadata?.attemptCount ?? "?"} recorded attempts</span>
          <InformationControl label="Attempt timeline">
            <p>Rows follow the producer&apos;s chronological ordering and stable tie-breaker. Page exhaustion does not imply an active or unknown attempt completed.</p>
            <p>Roles come from authoritative component/effect pairs, not IDs. Selecting a row opens metadata only; prompt, review, and process output remain explicit reads.</p>
            {metadata ? (
              <dl className="run-metadata">
                <div><dt>Short name</dt><dd>{metadata.shortName}</dd></div>
                <div><dt>Project</dt><dd>{metadata.projectName}</dd></div>
                <div><dt>Raw state</dt><dd>{metadata.state}</dd></div>
                <div><dt>Implementer</dt><dd>{metadata.implementerProvider}</dd></div>
                <div><dt>Submitted</dt><dd>{formatInstant(metadata.submittedAt, timeZone)}</dd></div>
                <div><dt>Updated</dt><dd>{formatInstant(metadata.updatedAt, timeZone)}</dd></div>
                <div><dt>Review budget</dt><dd>{metadata.reviewBudget.completed} / {metadata.reviewBudget.max}</dd></div>
                <div><dt>Pending Codex role / turn</dt><dd>{formatOptional(metadata.codexPendingRole)} / {formatOptional(metadata.codexPendingTurn)}</dd></div>
                <div><dt>Capacity reason</dt><dd>{formatOptional(metadata.codexCapacityReason)}</dd></div>
                <div><dt>Implementer retry wait</dt><dd>{metadata.implementerRetryWaitUntil === null ? "not recorded" : formatInstant(metadata.implementerRetryWaitUntil, timeZone)}</dd></div>
                <div><dt>Timeout retries</dt><dd>{metadata.implementerTimeoutAutomaticRetries ?? "not recorded"} / {metadata.implementerTimeoutRetryLimit ?? "not recorded"}</dd></div>
                <div><dt>Recorded next action</dt><dd>{metadata.safeNextAction.kind}</dd></div>
                <div><dt>Run ID</dt><dd>{metadata.runId}</dd></div>
              </dl>
            ) : inspect.loading ? <p className="loading-line">Reading selected run metadata…</p> : null}
            {selectedRun.sequenceBinding ? (
              <p className="lineage-line">
                {selectedRun.sequenceBinding.sequenceShortName} · {selectedRun.sequenceBinding.sequenceName} · {selectedRun.sequenceBinding.sequenceId} · phase {selectedRun.sequenceBinding.ordinal} {selectedRun.sequenceBinding.phaseName} · generation {selectedRun.sequenceBinding.generation} · {selectedRun.sequenceBinding.attemptKind}
                {selectedRun.sequenceBinding.sourceRunId ? ` · replaces ${selectedRun.sequenceBinding.sourceRunId}` : ""}
              </p>
            ) : <p className="lineage-line">Standalone run</p>}
            {timeline.observations.length ? (
              <ul>
                {timeline.observations.map((item) => (
                  <li key={item.offset}>
                    Offset {item.offset}: source observed {formatInstant(item.observedAt, timeZone)};
                    received {formatInstant(item.receivedAt, timeZone)} ({formatAge(item.receivedAt)}).
                  </li>
                ))}
              </ul>
            ) : null}
          </InformationControl>
        </div>
      </div>
      {inspect.error ? (
        <p className="error-banner" role="alert">
          Run metadata: {inspect.error}
          {inspect.observation
            ? ` · showing last success (${formatAge(inspect.observation.receivedAt)})`
            : ""}
        </p>
      ) : null}
      {timeline.error ? (
        <p className="error-banner" role="alert">
          Timeline: {timeline.error}
          {timeline.observations.length
            ? ` · showing last success (${formatAge(timeline.observations[0].receivedAt)})`
            : ""}
        </p>
      ) : null}
      {timeline.items.length ? (
        <>
          <DashboardChart
            ariaLabel={`Gantt timeline for ${selectedRun.runId}`}
            height={Math.max(320, timeline.items.length * 40 + 130)}
            onEvents={chartEvents}
            option={option}
            ownershipKey={selectedRun.runId}
            theme={theme}
          />
          <details
            key={selectedRun.runId}
            ref={attemptDisclosure}
            className="attempt-details-disclosure"
          >
            <summary ref={attemptSummary}>Attempt details</summary>
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
                  onClick={() => selectAttempt(row.attempt.attemptId)}
                >
                  <span className="run-row-title">{row.caption}</span>
                  <span>{row.role} · {row.attempt.status}</span>
                  <span>{row.attempt.component} · {row.attempt.effectKind ?? "no recorded effect"}</span>
                  <span>
                    {row.shape === "interval"
                      ? `${formatInstant(row.attempt.launchRequestedAt!, timeZone)} → ${formatInstant(row.attempt.completedAt!, timeZone)} · ${formatDuration(row.attempt.observedDurationSeconds)}`
                      : row.shape === "launch-marker"
                        ? `Launched ${formatInstant(row.attempt.launchRequestedAt!, timeZone)}; no observed end`
                        : `Created ${formatInstant(row.attempt.createdAt, timeZone)}; not launched`}
                  </span>
                  <span>Attempt ID: {row.attempt.attemptId}</span>
                </button>
              ))}
            </div>
          </details>
          {selectedAttempt ? (
            <>
              <p className="selected-attempt" aria-live="polite">
                Selected {attemptCaption(selectedAttempt, timeline.items.indexOf(selectedAttempt) + 1)}; metadata modal is open. No content was loaded; inspection still requires a deliberate control.
              </p>
              <AttemptModal
                key={`${selectedRun.runId}:${selectedAttempt.attemptId}`}
                attempt={selectedAttempt}
                caption={attemptCaption(
                  selectedAttempt,
                  timeline.items.indexOf(selectedAttempt) + 1,
                )}
                dataSource={contentDataSource}
                runId={selectedRun.runId}
                onClose={closeAttempt}
                timeZone={timeZone}
              />
            </>
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
      ) : null}
    </section>
  );
}
