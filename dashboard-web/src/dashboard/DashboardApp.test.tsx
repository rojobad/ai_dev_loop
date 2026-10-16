import React from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  CodexCapacityData,
  DashboardDataSource,
  DashboardFilters,
  DashboardProjectsData,
  DashboardSummaryData,
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

  async capacity(_signal: AbortSignal): Promise<SourceObservation<CodexCapacityData>> {
    return observation(capacityData);
  }
}

beforeEach(() => localStorage.clear());

describe("C263 overview shell", () => {
  it("retires old replies and leaves an invalid draft uncommitted", async () => {
    const dataSource = new DeferredDataSource();
    render(<DashboardApp dataSource={dataSource} />);
    expect(dataSource.requests).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("IANA time zone"), { target: { value: "UTC" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-01-01T00:00" } });
    fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
      target: { value: "2026-01-03T00:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(dataSource.requests).toHaveLength(2);

    await act(async () => dataSource.requests[1].resolve(observation(summaryData(7))));
    expect(await screen.findByLabelText("Runs started: 7")).toBeInTheDocument();
    await act(async () => dataSource.requests[0].resolve(observation(summaryData(1))));
    expect(screen.getByLabelText("Runs started: 7")).toBeInTheDocument();
    expect(screen.queryByLabelText("Runs started: 1")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-01-04T00:00" } });
    fireEvent.change(screen.getByLabelText("Until (exclusive)"), {
      target: { value: "2026-01-03T00:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Previous applied results remain unchanged");
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
    expect(screen.getByText("Other: 1")).toBeInTheDocument();
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
    await user.click(screen.getByRole("button", { name: "Apply" }));
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

  it("observes every capacity window once and never ties probes to filters", async () => {
    const user = userEvent.setup();
    const dataSource = new ImmediateDataSource();
    render(<DashboardApp dataSource={dataSource} />);
    expect(await screen.findByText("64% remaining · 36% used")).toBeInTheDocument();
    expect(screen.getByText("27% remaining · 73% used")).toBeInTheDocument();
    expect(screen.getByText("Reset unavailable")).toBeInTheDocument();
    expect(dataSource.capacityCalls).toBe(1);

    await user.click(screen.getByRole("button", { name: "Refresh applied scope" }));
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
});
