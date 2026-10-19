import React from "react";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  ActivityCalendar,
  activityCalendarOption,
  activityDisplayValue,
  calendarRange,
} from "./ActivityCalendar";
import { RefreshCoordinator } from "./refreshCoordinator";
import { ImmediateDataSource, completeCoverage, observation } from "./test-fixtures";
import type {
  DashboardActivityData,
  DashboardActivityMetric,
  DashboardFilters,
  SourceObservation,
} from "./types";

vi.mock("./DashboardChart", () => ({
  DashboardChart: ({ ariaLabel }: { ariaLabel: string }) => (
    <div role="img" aria-label={ariaLabel}>calendar chart</div>
  ),
}));

interface Deferred<T> {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

const filters: DashboardFilters = {
  fromUtc: "2028-02-28T04:00:00Z",
  toUtc: "2028-03-02T04:00:00Z",
  timeZone: "America/La_Paz",
  projectName: "provider-aware-project",
};

function data(
  metric: DashboardActivityMetric,
  days: Array<{ date: string; value: number }>,
  partial = false,
): DashboardActivityData {
  return {
    observedAt: "2028-03-02T04:00:01Z",
    filters: { ...filters, projectName: filters.projectName ?? null },
    coverage: partial
      ? {
          ...completeCoverage,
          dataState: "partial",
          missingDurationAttempts: 1,
          warnings: ["missing_duration_attempts"],
        }
      : completeCoverage,
    metric,
    unit: metric === "process-hours" ? "process-seconds" : "count",
    days,
    total: days.reduce((sum, item) => sum + item.value, 0),
  };
}

class HeldActivityDataSource extends ImmediateDataSource {
  readonly calls: Array<{ filters: DashboardFilters; metric: DashboardActivityMetric }> = [];
  readonly held = new Map<
    DashboardActivityMetric,
    Deferred<SourceObservation<DashboardActivityData>>
  >();

  override activity(
    selectedFilters: DashboardFilters,
    metric: DashboardActivityMetric,
  ): Promise<SourceObservation<DashboardActivityData>> {
    this.calls.push({ filters: selectedFilters, metric });
    const request = deferred<SourceObservation<DashboardActivityData>>();
    this.held.set(metric, request);
    return request.promise;
  }
}

describe("C266-1 activity calendar", () => {
  it("uses named-zone backend days, fences an old metric reply, and converts seconds once", async () => {
    const user = userEvent.setup();
    const dataSource = new HeldActivityDataSource();
    const coordinator = new RefreshCoordinator();
    render(
      <ActivityCalendar
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-1"
        filters={filters}
        theme="light"
      />,
    );
    expect(dataSource.calls[0]).toEqual({ filters, metric: "submissions" });

    await user.selectOptions(screen.getByLabelText("Activity metric"), "review-rounds");
    expect(dataSource.calls.at(-1)).toEqual({ filters, metric: "review-rounds" });
    await act(async () => {
      dataSource.held.get("review-rounds")?.resolve(
        observation(
          data("review-rounds", [
            { date: "2028-02-28", value: 1 },
            { date: "2028-02-29", value: 2 },
            { date: "2028-03-01", value: 1 },
          ]),
        ),
      );
    });
    expect(await screen.findByText("Review rounds total:")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Review rounds activity calendar/ })).toBeInTheDocument();
    const dailyDisclosure = screen.getByText("Show daily values").closest("details");
    expect(dailyDisclosure).not.toHaveAttribute("open");
    await user.click(screen.getByText("Show daily values"));
    expect(dailyDisclosure).toHaveAttribute("open");
    expect(screen.getByLabelText("Review rounds daily values")).toHaveTextContent(
      "29/02/20282",
    );

    await act(async () => {
      dataSource.held.get("submissions")?.resolve(
        observation(data("submissions", [{ date: "2028-02-28", value: 9 }])),
      );
    });
    expect(screen.getByText("Review rounds total:")).toBeInTheDocument();
    expect(screen.queryByText("Submissions total:")).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Activity metric"), "process-hours");
    await act(async () => {
      dataSource.held.get("process-hours")?.resolve(
        observation(
          data(
            "process-hours",
            [
              { date: "2028-02-28", value: 5400 },
              { date: "2028-03-01", value: 1800 },
            ],
            true,
          ),
        ),
      );
    });
    expect(await screen.findByText("02 h 00 m")).toBeInTheDocument();
    expect(screen.getByLabelText("Process hours daily values")).toHaveTextContent("01 h 30 m");
    expect(screen.getByText(/absent days are unknown/i)).toBeInTheDocument();
    expect(screen.queryByText(/Codex implementer attempts never become review rounds/)).not
      .toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Information about Activity calendar" }));
    expect(screen.getByRole("dialog", { name: "Activity calendar" })).toHaveTextContent(
      /implementer attempts never become review rounds/,
    );
  });

  it("reserves the previous calendar height while a new metric is pending", async () => {
    const user = userEvent.setup();
    const dataSource = new HeldActivityDataSource();
    const view = render(
      <ActivityCalendar
        coordinator={new RefreshCoordinator()}
        dataSource={dataSource}
        datasetKey="scope-1"
        filters={filters}
        theme="light"
      />,
    );
    await act(async () => {
      dataSource.held.get("submissions")?.resolve(
        observation(data("submissions", [{ date: "2028-02-28", value: 3 }])),
      );
    });
    const results = view.container.querySelector(".activity-results")!;
    vi.spyOn(results, "getBoundingClientRect").mockReturnValue(new DOMRect(0, 0, 800, 430));
    await user.selectOptions(screen.getByLabelText("Activity metric"), "review-rounds");
    expect(results).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("status")).toHaveTextContent("Reading activity");
    expect(view.container.querySelector(".activity-loading")).toHaveStyle({ minHeight: "430px" });
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText("Submissions total:")).not.toBeInTheDocument();
    await act(async () => {
      dataSource.held.get("review-rounds")?.resolve(
        observation(data("review-rounds", [{ date: "2028-02-28", value: 1 }])),
      );
    });
    expect(results).toHaveAttribute("aria-busy", "false");
    expect(screen.getByRole("img", { name: /Review rounds/ })).toBeInTheDocument();
  });

  it("keeps year/leap boundaries as named-zone calendar dates", () => {
    const value = data("process-hours", [
      { date: "2028-02-28", value: 3600 },
      { date: "2028-02-29", value: 7200 },
      { date: "2028-03-01", value: 1800 },
    ]);
    expect(calendarRange(value)).toEqual(["2028-02-28", "2028-03-01"]);
    expect(activityDisplayValue("process-hours", 5400)).toBe(1.5);
    const option = activityCalendarOption(value) as {
      series: Array<{ data: Array<[string, number]> }>;
    };
    expect(option.series[0].data).toEqual([
      ["2028-02-28", 1],
      ["2028-02-29", 2],
      ["2028-03-01", 0.5],
    ]);
    const tooltip = (activityCalendarOption(value).tooltip as {
      formatter(event: unknown): string;
    }).formatter;
    expect(tooltip({ value: ["2028-02-29", 2] })).toBe(
      "<strong>29/02/2028</strong><br>02 h 00 m",
    );
  });

  it("retains the original last success and labels its age after a refresh error", async () => {
    class RecoveringDataSource extends ImmediateDataSource {
      calls = 0;

      override async activity(
        _filters: DashboardFilters,
        metric: DashboardActivityMetric,
      ): Promise<SourceObservation<DashboardActivityData>> {
        this.calls += 1;
        if (this.calls > 1) throw new Error("server restarted");
        return observation(data(metric, [{ date: "2028-02-28", value: 3 }]));
      }
    }
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-03T00:00:02Z"));
    const coordinator = new RefreshCoordinator();
    try {
      render(
        <ActivityCalendar
          coordinator={coordinator}
          dataSource={new RecoveringDataSource()}
          datasetKey="scope-1"
          filters={filters}
          theme="light"
        />,
      );
      await act(async () => Promise.resolve());
      expect(screen.getByText("Submissions total:")).toBeInTheDocument();
      await act(async () => {
        coordinator.refreshAll();
        for (let index = 0; index < 8; index += 1) await Promise.resolve();
      });
      expect(screen.getByRole("alert")).toHaveTextContent("server restarted");
      expect(screen.getByRole("alert")).toHaveTextContent("showing the last successful observation");
      expect(screen.getByText(/Last successful activity read/)).toHaveTextContent("<1 m old");
      expect(screen.getByText("Submissions total:")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("preserves a compatible success across manual revision failure and clears a new scope", async () => {
    class RevisionDataSource extends ImmediateDataSource {
      requests: Array<{
        filters: DashboardFilters;
        request: Deferred<SourceObservation<DashboardActivityData>>;
      }> = [];

      override activity(
        requestedFilters: DashboardFilters,
        metric: DashboardActivityMetric,
      ): Promise<SourceObservation<DashboardActivityData>> {
        const request = deferred<SourceObservation<DashboardActivityData>>();
        this.requests.push({ filters: requestedFilters, request });
        expect(metric).toBe("submissions");
        return request.promise;
      }
    }

    const dataSource = new RevisionDataSource();
    const coordinator = new RefreshCoordinator();
    const view = render(
      <ActivityCalendar
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="revision-1"
        filters={filters}
        theme="light"
      />,
    );
    const first = {
      ...observation(data("submissions", [{ date: "2028-02-28", value: 3 }])),
      observedAt: "2028-03-02T04:00:02Z",
      receivedAt: "2028-03-02T04:00:03Z",
    };
    await act(async () => dataSource.requests[0].request.resolve(first));
    expect(await screen.findByText("Submissions total:")).toBeInTheDocument();
    expect(screen.getByText(/Last successful activity read/)).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Information about Activity calendar" }));
    expect(screen.getByRole("dialog", { name: "Activity calendar" })).toHaveTextContent(
      "Source observed 02/03/2028 00:00; received 02/03/2028 00:00.",
    );
    await user.click(screen.getByRole("button", { name: "Close" }));

    view.rerender(
      <ActivityCalendar
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="revision-2"
        filters={{ ...filters }}
        theme="light"
      />,
    );
    expect(screen.getByText("Submissions total:")).toBeInTheDocument();
    await act(async () => dataSource.requests[1].request.reject(new Error("manual refresh failed")));
    expect(screen.getByRole("alert")).toHaveTextContent("manual refresh failed");
    expect(screen.getByRole("alert")).toHaveTextContent("showing the last successful observation");
    expect(screen.getByText(/Last successful activity read/)).toBeInTheDocument();

    const otherFilters = { ...filters, projectName: "different-project" };
    view.rerender(
      <ActivityCalendar
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="revision-3"
        filters={otherFilters}
        theme="light"
      />,
    );
    expect(screen.queryByText("Submissions total:")).not.toBeInTheDocument();
    await act(async () => dataSource.requests[2].request.reject(new Error("new scope failed")));
    expect(screen.getByRole("alert")).toHaveTextContent("new scope failed");
    expect(screen.getByRole("alert")).not.toHaveTextContent("last successful observation");
  });
});
