import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AttemptTimelineData,
  CodexCapacityData,
  DashboardActivityData,
  DashboardActivityMetric,
  DashboardDataSource,
  DashboardFilters,
  DashboardProjectsData,
  DashboardRunsData,
  DashboardSummaryData,
  FrozenPromptChunk,
  ProcessOutputChunk,
  ProcessStream,
  ReviewContentChunk,
  ReviewContentKind,
  ReviewDetailData,
  RunInspectData,
  SourceObservation,
} from "./types";
import { capacityData, ImmediateDataSource, observation, summaryData } from "./test-fixtures";

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
      onClick={() => onEvents?.click({ name: "project-b", data: { projectName: "project-b" } })}
    >
      chart
    </button>
  ),
}));

import { DashboardApp } from "./DashboardApp";

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

class DeferredDataSource implements DashboardDataSource {
  readonly requests: Array<Deferred<SourceObservation<DashboardSummaryData>>> = [];

  async projects(
    _page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardProjectsData>> {
    return observation({
      observedAt: "2026-01-03T00:00:00Z",
      items: [{ projectName: "project-a", runCount: 1 }],
      page: { offset: 0, limit: 100, nextOffset: null, hasMore: false },
      totalCount: 1,
    });
  }

  summary(
    _filters: DashboardFilters,
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardSummaryData>> {
    const request = deferred<SourceObservation<DashboardSummaryData>>();
    this.requests.push(request);
    return request.promise;
  }

  async runs(
    filters: DashboardFilters,
    page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>> {
    return observation({
      observedAt: "2026-01-03T00:00:00Z",
      filters: { ...filters, projectName: filters.projectName ?? null },
      coverage: {
        dataState: "empty",
        unstartedRuns: 0,
        missingLaunchTimes: 0,
        unknownExecutionKinds: 0,
        missingDurationAttempts: 0,
        warnings: [],
      },
      items: [],
      page: { offset: page.offset, limit: page.limit, nextOffset: null, hasMore: false },
      totalCount: 0,
    });
  }

  async activity(
    filters: DashboardFilters,
    metric: DashboardActivityMetric,
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardActivityData>> {
    return observation({
      observedAt: "2026-01-03T00:00:00Z",
      filters: { ...filters, projectName: filters.projectName ?? null },
      coverage: {
        dataState: "empty",
        unstartedRuns: 0,
        missingLaunchTimes: 0,
        unknownExecutionKinds: 0,
        missingDurationAttempts: 0,
        warnings: [],
      },
      metric,
      unit: metric === "process-hours" ? "process-seconds" : "count",
      days: [],
      total: 0,
    });
  }

  async runInspect(
    _runId: string,
    _signal: AbortSignal,
  ): Promise<SourceObservation<RunInspectData>> {
    throw new Error("No run is selectable in this fixture.");
  }

  async runTimeline(
    _runId: string,
    _page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<AttemptTimelineData>> {
    throw new Error("No run is selectable in this fixture.");
  }

  async initialPrompt(
    _runId: string,
    _read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<FrozenPromptChunk>> {
    throw new Error("No run is selectable in this fixture.");
  }

  async review(
    _runId: string,
    _attemptId: string,
    _signal: AbortSignal,
  ): Promise<SourceObservation<ReviewDetailData>> {
    throw new Error("No run is selectable in this fixture.");
  }

  async reviewContent(
    _runId: string,
    _attemptId: string,
    _kind: ReviewContentKind,
    _read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<ReviewContentChunk>> {
    throw new Error("No run is selectable in this fixture.");
  }

  async runOutput(
    _runId: string,
    _attemptId: string,
    _stream: ProcessStream,
    _read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<ProcessOutputChunk>> {
    throw new Error("No run is selectable in this fixture.");
  }

  async capacity(_signal: AbortSignal): Promise<SourceObservation<CodexCapacityData>> {
    return observation(capacityData);
  }
}

beforeEach(() => localStorage.clear());

describe("C263 overview shell", () => {
  it("C283-1 commits one coherent typed draft after exactly 400 ms", async () => {
    vi.useFakeTimers();
    const dataSource = new DeferredDataSource();
    try {
      render(<DashboardApp dataSource={dataSource} />);
      await act(async () => {
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(dataSource.requests).toHaveLength(1);

      fireEvent.change(screen.getByLabelText("IANA time zone"), { target: { value: "UTC" } });
      fireEvent.change(screen.getByLabelText("From"), { target: { value: "01/01/2026 00:00" } });
      fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
        target: { value: "03/01/2026 00:00" },
      });
      await act(async () => vi.advanceTimersByTime(399));
      expect(dataSource.requests).toHaveLength(1);
      await act(async () => {
        vi.advanceTimersByTime(1);
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(dataSource.requests).toHaveLength(2);

      fireEvent.change(screen.getByLabelText("From"), { target: { value: "incomplete" } });
      await act(async () => {
        vi.advanceTimersByTime(400);
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(dataSource.requests).toHaveLength(2);
      expect(screen.getByRole("alert")).toHaveTextContent(
        "previous valid results remain unchanged",
      );

      fireEvent.change(screen.getByLabelText("Project"), {
        target: { value: "project-a" },
      });
      await act(async () => {
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(dataSource.requests).toHaveLength(3);
      expect(screen.getByRole("alert")).toHaveTextContent(
        "previous valid results remain unchanged",
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("C283-1 applies a range preset immediately without a delayed duplicate", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-10T12:00:00Z"));
    const dataSource = new ImmediateDataSource();
    try {
      render(<DashboardApp dataSource={dataSource} />);
      await act(async () => {
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(dataSource.summaryCalls).toHaveLength(1);
      fireEvent.click(screen.getByRole("button", { name: "30 days" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
        for (let index = 0; index < 24; index += 1) await Promise.resolve();
      });
      expect(dataSource.summaryCalls).toHaveLength(2);
      await act(async () => {
        vi.advanceTimersByTime(400);
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(dataSource.summaryCalls).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("C283-1 keeps an immediate project selected across an older typed debounce", async () => {
    vi.useFakeTimers();
    const dataSource = new ImmediateDataSource();
    try {
      render(<DashboardApp dataSource={dataSource} />);
      await act(async () => {
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(dataSource.summaryCalls).toHaveLength(1);

      fireEvent.change(screen.getByLabelText("IANA time zone"), { target: { value: "UTC" } });
      fireEvent.change(screen.getByLabelText("From"), { target: { value: "01/01/2026 00:00" } });
      fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
        target: { value: "03/01/2026 00:00" },
      });
      await act(async () => vi.advanceTimersByTime(399));
      fireEvent.change(screen.getByLabelText("Project"), {
        target: { value: "project-b" },
      });
      await act(async () => {
        vi.advanceTimersByTime(1);
        for (let index = 0; index < 12; index += 1) await Promise.resolve();
      });

      expect(dataSource.summaryCalls).toHaveLength(3);
      expect(dataSource.summaryCalls.at(-1)).toEqual({
        fromUtc: "2026-01-01T00:00:00.000Z",
        toUtc: "2026-01-03T00:00:00.000Z",
        timeZone: "UTC",
        projectName: "project-b",
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it("retires old replies and leaves an invalid draft uncommitted", async () => {
    const dataSource = new DeferredDataSource();
    render(<DashboardApp dataSource={dataSource} />);
    await waitFor(() => expect(dataSource.requests).toHaveLength(1));

    fireEvent.change(screen.getByLabelText("IANA time zone"), { target: { value: "UTC" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "01/01/2026 00:00" } });
    fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
      target: { value: "03/01/2026 00:00" },
    });
    await waitFor(() => expect(dataSource.requests).toHaveLength(2));
    expect(screen.queryByRole("button", { name: "Apply" })).not.toBeInTheDocument();

    await act(async () => dataSource.requests[1].resolve(observation(summaryData(7))));
    expect(await screen.findByLabelText("Runs started: 7")).toBeInTheDocument();
    await act(async () => dataSource.requests[0].resolve(observation(summaryData(1))));
    expect(screen.getByLabelText("Runs started: 7")).toBeInTheDocument();
    expect(screen.queryByLabelText("Runs started: 1")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "04/01/2026 00:00" } });
    fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
      target: { value: "03/01/2026 00:00" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("previous valid results remain unchanged");
    expect(dataSource.requests).toHaveLength(2);
    expect(screen.getByLabelText("Runs started: 7")).toBeInTheDocument();
  });

  it("renders independent summary semantics and gives charts keyboard-equivalent selection", async () => {
    const user = userEvent.setup();
    const dataSource = new ImmediateDataSource();
    render(<DashboardApp dataSource={dataSource} />);
    expect(await screen.findByLabelText("Runs started: 7")).toBeInTheDocument();
    expect(screen.getByLabelText("Completed: 2")).toBeInTheDocument();
    expect(screen.getByLabelText("Completed with residual risk: 1")).toBeInTheDocument();
    expect(screen.getByText(/Other: 1/)).toHaveTextContent(
      'Other: 1 · <future-state onmouseover="bad"> (1)',
    );
    expect(screen.getAllByText("project-a<script>alert(1)</script>").length).toBeGreaterThan(0);
    expect(document.querySelector("script:not([src])")).toBeNull();

    const keyboardButton = screen.getByRole("button", { name: /project-b 3 started/ });
    keyboardButton.focus();
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(dataSource.summaryCalls.at(-1)?.projectName).toBe("project-b"),
    );
    expect(await screen.findByLabelText("Runs started: 3")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Project"), "");
    await waitFor(() => expect(dataSource.summaryCalls.at(-1)?.projectName).toBeUndefined());
    expect(await screen.findByLabelText("Runs started: 7")).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", {
        name: "Stacked horizontal chart of started runs by project and state",
      }),
    );
    await waitFor(() =>
      expect(dataSource.summaryCalls.at(-1)?.projectName).toBe("project-b"),
    );
    expect(await screen.findByLabelText("Runs started: 3")).toBeInTheDocument();
  });

  it("C282-4 commits DD/MM drafts in the selected zone and never submits a guessed fold", async () => {
    const dataSource = new ImmediateDataSource();
    render(<DashboardApp dataSource={dataSource} />);
    await waitFor(() => expect(dataSource.summaryCalls).toHaveLength(1));

    fireEvent.change(screen.getByLabelText("IANA time zone"), {
      target: { value: "America/New_York" },
    });
    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "01/11/2026 01:30" },
    });
    fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
      target: { value: "01/11/2026 02:30" },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("ambiguous");
    expect(dataSource.summaryCalls).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "01/11/2026 01:30-04:00" },
    });
    fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
      target: { value: "01/11/2026 02:30-05:00" },
    });
    await waitFor(() => expect(dataSource.summaryCalls).toHaveLength(2));
    expect(dataSource.summaryCalls[1]).toEqual({
      fromUtc: "2026-11-01T05:30:00.000Z",
      toUtc: "2026-11-01T07:30:00.000Z",
      timeZone: "America/New_York",
    });
  });

  it("observes every capacity window once and never ties probes to filters", async () => {
    const user = userEvent.setup();
    const dataSource = new ImmediateDataSource();
    render(<DashboardApp dataSource={dataSource} />);
    expect(await screen.findByText("64% remaining · 36% used")).toBeInTheDocument();
    expect(screen.getByText("27% remaining · 73% used")).toBeInTheDocument();
    expect(screen.getByText("Reset unavailable")).toBeInTheDocument();
    expect(dataSource.capacityCalls).toBe(1);

    await user.click(screen.getByRole("button", { name: "Refresh current scope" }));
    await user.click(screen.getByRole("button", { name: /Inspect project-b/ }));
    await waitFor(() => expect(dataSource.summaryCalls.length).toBeGreaterThanOrEqual(3));
    expect(dataSource.capacityCalls).toBe(1);
  });

  it("renders unavailable capacity as unavailable rather than a synthetic zero", async () => {
    class UnavailableDataSource extends ImmediateDataSource {
      override async capacity(
        _signal: AbortSignal,
      ): Promise<SourceObservation<CodexCapacityData>> {
        this.capacityCalls += 1;
        return observation({ status: "unavailable", reason: "timeout", limits: [] });
      }
    }
    render(<DashboardApp dataSource={new UnavailableDataSource()} />);
    expect(await screen.findByText("Capacity unavailable · timeout")).toBeInTheDocument();
    expect(screen.queryByText(/0% remaining/)).not.toBeInTheDocument();
  });

  it("clears exact run and attempt ownership when an immediate project change retires scope", async () => {
    const user = userEvent.setup();
    const dataSource = new ImmediateDataSource();
    render(<DashboardApp dataSource={dataSource} />);
    await user.selectOptions(await screen.findByLabelText("Select loaded run"), "standalone-run");
    expect(await screen.findByText(/Selected run:/)).toHaveTextContent("standalone-run");
    expect(await screen.findByText("Attempt ID: attempt-cursor")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Project"), "project-b");
    await waitFor(() =>
      expect(screen.queryByText(/Selected run:/)).not.toBeInTheDocument(),
    );
    expect(
      screen.getByText("Select an exact run cell or the loaded-run control to inspect it."),
    ).toBeInTheDocument();
  });
});
