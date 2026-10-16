import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  AttemptTimelineData,
  AttemptTimelineItem,
  DashboardRunItem,
  RunInspectData,
  SourceObservation,
} from "./types";
import { DashboardDataSourceError } from "./types";
import { RefreshCoordinator } from "./refreshCoordinator";
import {
  ImmediateDataSource,
  inspectData,
  observation,
  standaloneRun,
} from "./test-fixtures";

vi.mock("./DashboardChart", () => ({
  DashboardChart: ({
    ariaLabel,
    onEvents,
  }: {
    ariaLabel: string;
    onEvents?: Record<string, (event: unknown) => void>;
  }) => (
    <button
      type="button"
      aria-label={ariaLabel}
      onClick={() => onEvents?.click({ data: { attemptId: "review-retry" } })}
    >
      timeline chart
    </button>
  ),
}));

import {
  attemptIdFromChartEvent,
  attemptRole,
  AttemptTimeline,
  buildTimelineRows,
} from "./AttemptTimeline";

function attempt(
  attemptId: string,
  component: string,
  effectKind: string | null,
  launchRequestedAt: string | null,
  completedAt: string | null,
  phaseAttempt = 1,
): AttemptTimelineItem {
  return {
    attemptId,
    component,
    effectKind,
    iteration: 1,
    phaseAttempt,
    status: completedAt === null ? "active" : "completed",
    createdAt: "2026-01-01T00:00:00Z",
    launchRequestedAt,
    completedAt,
    observedDurationSeconds:
      launchRequestedAt !== null && completedAt !== null
        ? (Date.parse(completedAt) - Date.parse(launchRequestedAt)) / 1000
        : null,
  };
}

const semanticAttempts = [
  attempt(
    "cursor-implementer",
    "cursor",
    "cursor.run_turn",
    "2026-01-01T01:00:00Z",
    "2026-01-01T03:00:00Z",
  ),
  attempt(
    "codex-implementer",
    "codex_implementer",
    "codex.implementer_bootstrap",
    "2026-01-01T01:30:00Z",
    "2026-01-01T02:30:00Z",
  ),
  attempt(
    "codex-reviewer",
    "codex",
    "codex.bootstrap_review",
    "2026-01-01T04:00:00Z",
    "2026-01-01T04:30:00Z",
  ),
  attempt(
    "active-implementer",
    "codex_implementer",
    "codex.implementer_resume",
    "2026-01-01T06:00:00Z",
    null,
    2,
  ),
  attempt("created-only", "cursor", "cursor.run_turn", null, null, 3),
];

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((innerResolve) => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}

class HeldSelectionDataSource extends ImmediateDataSource {
  readonly inspectRequests = new Map<string, Deferred<SourceObservation<RunInspectData>>>();
  readonly timelineRequests = new Map<string, Deferred<SourceObservation<AttemptTimelineData>>>();
  readonly aborted = new Map<string, AbortSignal>();

  override runInspect(runId: string, signal: AbortSignal) {
    const request = deferred<SourceObservation<RunInspectData>>();
    this.inspectRequests.set(runId, request);
    this.aborted.set(`inspect:${runId}`, signal);
    return request.promise;
  }

  override runTimeline(
    runId: string,
    _page: { offset: number; limit: number },
    signal: AbortSignal,
  ) {
    const request = deferred<SourceObservation<AttemptTimelineData>>();
    this.timelineRequests.set(runId, request);
    this.aborted.set(`timeline:${runId}`, signal);
    return request.promise;
  }
}

const olderRun: DashboardRunItem = { ...standaloneRun, runId: "older-replacement" };
const currentRun: DashboardRunItem = { ...standaloneRun, runId: "current-replacement" };

describe("C264-2 selected run ownership and race fencing", () => {
  it("aborts the old run, targets exact IDs, and ignores held late replies", async () => {
    const dataSource = new HeldSelectionDataSource();
    const view = render(
      <AttemptTimeline coordinator={new RefreshCoordinator()} dataSource={dataSource} selectedRun={olderRun} theme="light" />,
    );
    expect(dataSource.inspectRequests.has("older-replacement")).toBe(true);
    expect(dataSource.timelineRequests.has("older-replacement")).toBe(true);

    view.rerender(
      <AttemptTimeline coordinator={new RefreshCoordinator()} dataSource={dataSource} selectedRun={currentRun} theme="light" />,
    );
    expect(dataSource.aborted.get("inspect:older-replacement")?.aborted).toBe(true);
    expect(dataSource.inspectRequests.has("current-replacement")).toBe(true);
    expect(dataSource.timelineRequests.has("current-replacement")).toBe(true);

    await act(async () => {
      dataSource.inspectRequests.get("current-replacement")?.resolve(
        observation(inspectData("current-replacement")),
      );
      dataSource.timelineRequests.get("current-replacement")?.resolve(
        observation({
          runId: "current-replacement",
          items: [semanticAttempts[0]],
          page: { offset: 0, limit: 25, nextOffset: null, hasMore: false },
        }),
      );
    });
    expect(await screen.findByText(/Selected run:/)).toHaveTextContent("current-replacement");
    expect(screen.getByText("cursor-implementer")).toBeInTheDocument();

    await act(async () => {
      dataSource.inspectRequests.get("older-replacement")?.resolve(
        observation(inspectData("older-replacement")),
      );
      dataSource.timelineRequests.get("older-replacement")?.resolve(
        observation({
          runId: "older-replacement",
          items: [attempt("late-old", "cursor", "cursor.run_turn", null, null)],
          page: { offset: 0, limit: 25, nextOffset: null, hasMore: false },
        }),
      );
    });
    expect(screen.queryByText("late-old")).not.toBeInTheDocument();
    expect(screen.getByText(/Selected run:/)).toHaveTextContent("current-replacement");
  });

  it("keeps the selected run metadata owner alive while paging only the timeline", async () => {
    const user = userEvent.setup();
    const metadata = deferred<SourceObservation<RunInspectData>>();
    let metadataSignal: AbortSignal | undefined;
    const timelineSignals: AbortSignal[] = [];
    class IndependentReadDataSource extends ImmediateDataSource {
      override runInspect(_runId: string, signal: AbortSignal) {
        metadataSignal = signal;
        return metadata.promise;
      }

      override async runTimeline(
        runId: string,
        page: { offset: number; limit: number },
        signal: AbortSignal,
      ): Promise<SourceObservation<AttemptTimelineData>> {
        timelineSignals.push(signal);
        return observation({
          runId,
          items: page.offset === 0 ? [semanticAttempts[0]] : [semanticAttempts[1]],
          page: page.offset === 0
            ? { offset: 0, limit: 25, nextOffset: 25, hasMore: true }
            : { offset: 25, limit: 25, nextOffset: null, hasMore: false },
        });
      }
    }

    render(
      <AttemptTimeline
        coordinator={new RefreshCoordinator()}
        dataSource={new IndependentReadDataSource()}
        selectedRun={standaloneRun}
        theme="light"
      />,
    );
    await user.click(await screen.findByRole("button", { name: "Load more attempts" }));
    expect(timelineSignals).toHaveLength(2);
    expect(timelineSignals[0].aborted).toBe(false);
    expect(metadataSignal?.aborted).toBe(false);

    await act(async () => {
      metadata.resolve(observation(inspectData("standalone-run")));
    });
    expect(await screen.findByText("project-a")).toBeInTheDocument();
  });

  it("rejects metadata whose returned run ID is not the exact selection", async () => {
    class MismatchedMetadataDataSource extends ImmediateDataSource {
      override async runInspect(): Promise<SourceObservation<RunInspectData>> {
        return observation(inspectData("different-run"));
      }
    }

    render(
      <AttemptTimeline
        coordinator={new RefreshCoordinator()}
        dataSource={new MismatchedMetadataDataSource()}
        selectedRun={standaloneRun}
        theme="light"
      />,
    );
    expect(await screen.findByText(/Run metadata:/)).toHaveTextContent(
      "did not match the selected run ID",
    );
  });

  it("retries transient service saturation for the current exact run at normal cadence", async () => {
    vi.useFakeTimers();
    class SaturatedThenReadyDataSource extends ImmediateDataSource {
      inspectCalls = 0;
      timelineCalls = 0;

      override async runInspect(runId: string): Promise<SourceObservation<RunInspectData>> {
        this.inspectCalls += 1;
        if (this.inspectCalls === 1) {
          throw new DashboardDataSourceError("SERVICE_UNAVAILABLE", "busy");
        }
        return observation(inspectData(runId));
      }

      override async runTimeline(
        runId: string,
      ): Promise<SourceObservation<AttemptTimelineData>> {
        this.timelineCalls += 1;
        if (this.timelineCalls === 1) {
          throw new DashboardDataSourceError("SERVICE_UNAVAILABLE", "busy");
        }
        return observation({
          runId,
          items: [semanticAttempts[0]],
          page: { offset: 0, limit: 25, nextOffset: null, hasMore: false },
        });
      }
    }

    const dataSource = new SaturatedThenReadyDataSource();
    const view = render(
      <AttemptTimeline coordinator={new RefreshCoordinator()} dataSource={dataSource} selectedRun={standaloneRun} theme="light" />,
    );
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(dataSource.inspectCalls).toBe(2);
      expect(dataSource.timelineCalls).toBe(2);
      expect(screen.getByText("cursor-implementer")).toBeInTheDocument();
      expect(screen.getByText("project-a")).toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("cancels a saturated old selection before its next retry", async () => {
    vi.useFakeTimers();
    class SaturatedOldDataSource extends ImmediateDataSource {
      inspectCalls = new Map<string, number>();
      timelineCalls = new Map<string, number>();

      override async runInspect(runId: string): Promise<SourceObservation<RunInspectData>> {
        this.inspectCalls.set(runId, (this.inspectCalls.get(runId) ?? 0) + 1);
        if (runId === olderRun.runId) {
          throw new DashboardDataSourceError("SERVICE_UNAVAILABLE", "busy");
        }
        return observation(inspectData(runId));
      }

      override async runTimeline(
        runId: string,
      ): Promise<SourceObservation<AttemptTimelineData>> {
        this.timelineCalls.set(runId, (this.timelineCalls.get(runId) ?? 0) + 1);
        if (runId === olderRun.runId) {
          throw new DashboardDataSourceError("SERVICE_UNAVAILABLE", "busy");
        }
        return observation({
          runId,
          items: [semanticAttempts[0]],
          page: { offset: 0, limit: 25, nextOffset: null, hasMore: false },
        });
      }
    }

    const dataSource = new SaturatedOldDataSource();
    const view = render(
      <AttemptTimeline coordinator={new RefreshCoordinator()} dataSource={dataSource} selectedRun={olderRun} theme="light" />,
    );
    try {
      await act(async () => {
        await Promise.resolve();
      });
      view.rerender(
        <AttemptTimeline coordinator={new RefreshCoordinator()} dataSource={dataSource} selectedRun={currentRun} theme="light" />,
      );
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      expect(dataSource.inspectCalls.get(olderRun.runId)).toBe(1);
      expect(dataSource.timelineCalls.get(olderRun.runId)).toBe(1);
      expect(dataSource.inspectCalls.get(currentRun.runId)).toBe(1);
      expect(dataSource.timelineCalls.get(currentRun.runId)).toBe(1);
      expect(screen.getByText(/Selected run:/)).toHaveTextContent(currentRun.runId);
      expect(screen.getByText("cursor-implementer")).toBeInTheDocument();
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });

  it("reports an explicit saturation error without an immediate retry loop", async () => {
    vi.useFakeTimers();
    class AlwaysSaturatedDataSource extends ImmediateDataSource {
      inspectCalls = 0;
      timelineCalls = 0;

      override async runInspect(): Promise<SourceObservation<RunInspectData>> {
        this.inspectCalls += 1;
        throw new DashboardDataSourceError("SERVICE_UNAVAILABLE", "busy");
      }

      override async runTimeline(): Promise<SourceObservation<AttemptTimelineData>> {
        this.timelineCalls += 1;
        throw new DashboardDataSourceError("SERVICE_UNAVAILABLE", "busy");
      }
    }

    const dataSource = new AlwaysSaturatedDataSource();
    const view = render(
      <AttemptTimeline
        coordinator={new RefreshCoordinator()}
        dataSource={dataSource}
        selectedRun={standaloneRun}
        theme="light"
      />,
    );
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3_101);
      });
      expect(screen.getByText(/Run metadata:/)).toHaveTextContent("busy");
      expect(screen.getByText(/Timeline:/)).toHaveTextContent("busy");
      expect(dataSource.inspectCalls).toBe(1);
      expect(dataSource.timelineCalls).toBe(1);
    } finally {
      view.unmount();
      vi.useRealTimers();
    }
  });
});

class PagedTimelineDataSource extends ImmediateDataSource {
  readonly calls: Array<{ runId: string; offset: number }> = [];
  firstPageReads = 0;

  override async runInspect(runId: string): Promise<SourceObservation<RunInspectData>> {
    return observation({
      ...inspectData(runId),
      implementerProvider: "codex",
      codexPendingRole: "implementer",
      codexPendingTurn: "correction",
      codexCapacityReason: "provider_quota",
      implementerRetryWaitUntil: "2026-01-01T08:00:00Z",
      implementerTimeoutAutomaticRetries: 2,
      implementerTimeoutRetryLimit: 3,
      attemptCount: 26,
    });
  }

  override async runTimeline(
    runId: string,
    page: { offset: number; limit: number },
  ): Promise<SourceObservation<AttemptTimelineData>> {
    this.calls.push({ runId, offset: page.offset });
    if (page.offset === 0) this.firstPageReads += 1;
    const fillers = Array.from({ length: 20 }, (_, index) =>
      attempt(`other-${index}`, "cursor", "cursor.create_chat", null, null, index + 4),
    );
    const firstPage = [...semanticAttempts, ...fillers];
    const reviewRetry = attempt(
      "review-retry",
      "codex",
      "codex.resume_review",
      "2026-01-01T07:00:00Z",
      "2026-01-01T07:15:00Z",
      2,
    );
    const result = observation({
      runId,
      items: page.offset === 0 ? firstPage : [reviewRetry],
      page: page.offset === 0
        ? { offset: 0, limit: 25, nextOffset: 25, hasMore: true }
        : { offset: 25, limit: 25, nextOffset: null, hasMore: false },
    });
    return {
      ...result,
      observedAt: page.offset === 0
        ? `2026-01-03T00:0${this.firstPageReads - 1}:00Z`
        : "2026-01-03T00:00:25Z",
      receivedAt: page.offset === 0
        ? `2026-01-03T00:0${this.firstPageReads - 1}:01Z`
        : "2026-01-03T00:00:26Z",
    };
  }
}

describe("C264-3 attempt timeline semantics", () => {
  it("positions intervals independently and keeps unknown durations as explicit markers", () => {
    const rows = buildTimelineRows(semanticAttempts);
    expect(rows.map((row) => ({
      id: row.attempt.attemptId,
      role: row.role,
      start: row.startMs,
      end: row.endMs,
      shape: row.shape,
    }))).toEqual([
      {
        id: "cursor-implementer",
        role: "Cursor implementer",
        start: Date.parse("2026-01-01T01:00:00Z"),
        end: Date.parse("2026-01-01T03:00:00Z"),
        shape: "interval",
      },
      {
        id: "codex-implementer",
        role: "Codex implementer",
        start: Date.parse("2026-01-01T01:30:00Z"),
        end: Date.parse("2026-01-01T02:30:00Z"),
        shape: "interval",
      },
      {
        id: "codex-reviewer",
        role: "Codex reviewer",
        start: Date.parse("2026-01-01T04:00:00Z"),
        end: Date.parse("2026-01-01T04:30:00Z"),
        shape: "interval",
      },
      {
        id: "active-implementer",
        role: "Codex implementer",
        start: Date.parse("2026-01-01T06:00:00Z"),
        end: null,
        shape: "launch-marker",
      },
      {
        id: "created-only",
        role: "Cursor implementer",
        start: Date.parse("2026-01-01T00:00:00Z"),
        end: null,
        shape: "created-marker",
      },
    ]);
    expect(attemptRole(attempt("not-review", "codex", "codex.implementer_resume", null, null)))
      .toBe("Non-execution / unclassified");
    expect(attemptIdFromChartEvent({ data: { attemptId: "codex-reviewer" } })).toBe(
      "codex-reviewer",
    );
  });

  it("loads explicit pages and selects the same typed IDs through rows and chart", async () => {
    const user = userEvent.setup();
    const dataSource = new PagedTimelineDataSource();
    render(<AttemptTimeline coordinator={new RefreshCoordinator()} dataSource={dataSource} selectedRun={standaloneRun} theme="light" />);
    expect(await screen.findByText("Codex implementer · active")).toBeInTheDocument();
    expect(screen.getByText(/Launched 2026-01-01T06:00:00Z; no observed end/)).toBeInTheDocument();
    expect(screen.getAllByText(/Created 2026-01-01T00:00:00Z; not launched/)).toHaveLength(21);
    expect(screen.getByText("implementer / correction")).toBeInTheDocument();
    expect(dataSource.calls).toEqual([{ runId: "standalone-run", offset: 0 }]);

    await user.click(screen.getByRole("button", { name: /^codex-implementer Codex/ }));
    expect(screen.getByText(/Selected attempt codex-implementer/)).toHaveTextContent(
      "No content was loaded",
    );

    await user.click(screen.getByRole("button", { name: "Load more attempts" }));
    expect(await screen.findByText("review-retry")).toBeInTheDocument();
    expect(dataSource.calls).toEqual([
      { runId: "standalone-run", offset: 0 },
      { runId: "standalone-run", offset: 25 },
    ]);
    await user.click(screen.getByRole("button", { name: "Gantt timeline for standalone-run" }));
    expect(screen.getByText(/Selected attempt review-retry/)).toHaveTextContent(
      "No content was loaded",
    );
    expect(screen.getByText(/page exhaustion does not complete an attempt/)).toBeInTheDocument();
  });

  it("retains historic-page dates without polling that page on first-page refresh", async () => {
    const user = userEvent.setup();
    const dataSource = new PagedTimelineDataSource();
    const coordinator = new RefreshCoordinator();
    render(
      <AttemptTimeline
        coordinator={coordinator}
        dataSource={dataSource}
        selectedRun={standaloneRun}
        theme="light"
      />,
    );
    await user.click(await screen.findByRole("button", { name: "Load more attempts" }));
    expect(await screen.findByText("review-retry")).toBeInTheDocument();

    for (let refresh = 0; refresh < 4; refresh += 1) {
      coordinator.refreshAll();
      await waitFor(() => expect(dataSource.firstPageReads).toBe(refresh + 2));
    }

    expect(dataSource.calls.map((call) => call.offset)).toEqual([0, 25, 0, 0, 0, 0]);
    expect(screen.getByText("review-retry")).toBeInTheDocument();
    const observations = screen.getByText(/Timeline page observations:/);
    expect(observations).toHaveTextContent("offset 0 · 2026-01-03T00:04:00Z");
    expect(observations).toHaveTextContent("offset 25 · 2026-01-03T00:00:25Z");
    expect(observations).not.toHaveTextContent("offset 0 · 2026-01-03T00:00:00Z");
  });
});
