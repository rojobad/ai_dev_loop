import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  DashboardFilters,
  DashboardRunItem,
  DashboardRunsData,
  SourceObservation,
} from "./types";
import { DashboardDataSourceError } from "./types";
import { completeCoverage, ImmediateDataSource, observation } from "./test-fixtures";
import { SUMMARY_REFRESH_MS, RefreshCoordinator } from "./refreshCoordinator";

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
      onClick={() => onEvents?.click({ data: { runId: "old-replacement" } })}
    >
      map chart
    </button>
  ),
}));

import { groupRunMapItems, RunMap, runIdFromChartEvent, runMapOption } from "./RunMap";

const filters: DashboardFilters = {
  fromUtc: "2026-01-01T00:00:00.000Z",
  toUtc: "2026-01-03T00:00:00.000Z",
  timeZone: "UTC",
};

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

class RunMapTimerRuntime {
  time = 0;
  nextId = 1;
  timers = new Map<number, { due: number; callback: () => void }>();

  now(): number {
    return this.time;
  }

  setTimeout(callback: () => void, delayMs: number): number {
    const id = this.nextId++;
    this.timers.set(id, { due: this.time + delayMs, callback });
    return id;
  }

  clearTimeout(handle: unknown): void {
    this.timers.delete(handle as number);
  }

  async advance(milliseconds: number): Promise<void> {
    const target = this.time + milliseconds;
    for (;;) {
      const next = [...this.timers.entries()]
        .filter(([, timer]) => timer.due <= target)
        .sort((left, right) => left[1].due - right[1].due || left[0] - right[0])[0];
      if (!next) break;
      this.time = next[1].due;
      this.timers.delete(next[0]);
      next[1].callback();
      for (let index = 0; index < 12; index += 1) await Promise.resolve();
    }
    this.time = target;
    for (let index = 0; index < 12; index += 1) await Promise.resolve();
  }
}

function run(
  runId: string,
  index: number,
  binding: DashboardRunItem["sequenceBinding"] = null,
): DashboardRunItem {
  const submittedAt = new Date(Date.UTC(2026, 0, 1, 0, index)).toISOString();
  const firstExecutionLaunchAt = new Date(Date.UTC(2026, 0, 1, 1, index)).toISOString();
  return {
    runId,
    shortName: runId,
    projectName: "project-a",
    implementerProvider: index % 2 ? "codex" : "cursor",
    state: index % 3 ? "completed" : "waiting_codex_capacity",
    submittedAt,
    firstExecutionLaunchAt,
    sequenceBinding: binding,
  };
}

const replacementBinding = {
  sequenceId: "sequence-authority",
  sequenceName: "Authoritative sequence",
  sequenceShortName: "Authority",
  ordinal: 2,
  phaseName: "implementation",
  generation: 1,
  attemptKind: "planned_run",
  sourceRunId: null,
};

const allRuns = [
  run("old-replacement", 0, replacementBinding),
  run("new-replacement", 1, {
    ...replacementBinding,
    generation: 2,
    attemptKind: "implementer_retry",
    sourceRunId: "old-replacement",
  }),
  ...Array.from({ length: 26 }, (_, index) => run(`standalone-${index}`, index + 2)),
];

class PagedDataSource extends ImmediateDataSource {
  readonly runCalls: Array<{ filters: DashboardFilters; offset: number; limit: number }> = [];
  firstPageReads = 0;

  override async runs(
    requestedFilters: DashboardFilters,
    page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>> {
    this.runCalls.push({ filters: requestedFilters, offset: page.offset, limit: page.limit });
    if (page.limit > 25) {
      throw new DashboardDataSourceError(
        "PAGE_ENVELOPE_TOO_LARGE",
        "retry with a smaller run page",
      );
    }
    if (page.offset === 0) this.firstPageReads += 1;
    const items = allRuns.slice(page.offset, page.offset + page.limit);
    const nextOffset = page.offset + page.limit < allRuns.length
      ? page.offset + page.limit
      : null;
    const result = observation({
      observedAt: page.offset === 0 ? "2026-01-03T00:00:00Z" : "2026-01-03T00:00:02Z",
      filters: {
        ...requestedFilters,
        fromUtc: requestedFilters.fromUtc.replace(".000Z", "Z"),
        toUtc: requestedFilters.toUtc.replace(".000Z", "Z"),
        projectName: requestedFilters.projectName ?? null,
      },
      coverage: completeCoverage,
      items,
      page: {
        offset: page.offset,
        limit: page.limit,
        nextOffset,
        hasMore: nextOffset !== null,
      },
      totalCount: allRuns.length,
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

describe("C282-1 compact authoritative run map", () => {
  it("starts without visible run cards, keeps generations distinct, and selects exact IDs", async () => {
    const user = userEvent.setup();
    const dataSource = new PagedDataSource();
    const select = vi.fn();
    render(
      <RunMap
        coordinator={new RefreshCoordinator()}
        dataSource={dataSource}
        filters={filters}
        datasetKey="scope-1"
        selectedRunId={null}
        onSelectRun={select}
        theme="light"
      />,
    );

    expect(await screen.findByText("28/28 loaded")).toBeInTheDocument();
    expect(document.querySelector(".run-map-rows")).toBeNull();
    expect(screen.getByRole("option", { name: /old-replacement · cursor · old-replacement/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /new-replacement · codex · new-replacement/ }))
      .toBeInTheDocument();
    expect(select).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Load more runs" })).not.toBeInTheDocument();
    expect(dataSource.runCalls[0]).toEqual({ filters, offset: 0, limit: 1000 });
    expect(dataSource.runCalls.map((call) => call.limit)).toEqual([
      1000, 500, 250, 125, 62, 31, 15, 15,
    ]);
    expect(dataSource.runCalls.map((call) => call.offset)).toEqual([
      0, 0, 0, 0, 0, 0, 0, 15,
    ]);

    await user.click(
      screen.getByRole("button", { name: "Information about Sequence and run map" }),
    );
    expect(screen.getByRole("dialog", { name: "Sequence and run map" })).toHaveTextContent(
      "not presented as one atomic snapshot",
    );
    await user.click(screen.getByRole("button", { name: "Close" }));

    await user.selectOptions(screen.getByLabelText("Select loaded run"), "new-replacement");
    expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ runId: "new-replacement" }));
    await user.click(
      screen.getByRole("button", { name: "Selectable chart cells for authoritative run IDs" }),
    );
    expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ runId: "old-replacement" }));
    await user.selectOptions(screen.getByLabelText("Select loaded run"), "");
    expect(select).toHaveBeenLastCalledWith(null);
  });

  it("groups only authoritative bindings and never derives lineage from run names", () => {
    const groups = groupRunMapItems(allRuns.slice(0, 3));
    expect(groups[0].key).toBe("sequence-authority");
    expect(groups[0].label).toBe("Authority");
    expect(groups[0].details).toContain("Authoritative sequence");
    expect(groups[0].items.map((item) => item.runId)).toEqual([
      "old-replacement",
      "new-replacement",
    ]);
    expect(groups[1].key).toBe("standalone");
    expect(runIdFromChartEvent({ data: { runId: "old-replacement" } })).toBe(
      "old-replacement",
    );
    expect(runIdFromChartEvent({ name: "new-replacement" })).toBeNull();
  });

  it("escapes malicious aliases while duplicate aliases retain exact run IDs", () => {
    const malicious = '<img src=x onerror="boom">';
    const first = { ...run("exact-a", 1), shortName: malicious };
    const second = { ...run("exact-b", 2), shortName: malicious };
    const option = runMapOption([first, second]);
    const formatter = (option.tooltip as { formatter: (value: unknown) => string }).formatter;
    const series = option.series as Array<{
      data: Array<{ runId: string; shortName: string }>;
    }>;
    expect(series[0].data.map((item) => item.runId)).toEqual(["exact-a", "exact-b"]);
    expect(runIdFromChartEvent({ data: series[0].data[0] })).toBe("exact-a");
    expect(runIdFromChartEvent({ data: series[0].data[1] })).toBe("exact-b");
    expect(formatter({ data: series[0].data[0] })).toContain(
      "&lt;img src=x onerror=&quot;boom&quot;&gt;",
    );
    expect(formatter({ data: series[0].data[0] })).not.toContain(malicious);
    expect(formatter({ data: series[0].data[0] })).not.toContain("exact-a");
    expect(formatter({ data: series[0].data[0] })).not.toMatch(/Run ID:|Sequence ID:/);
    expect(option.xAxis).not.toHaveProperty("name");
  });

  it("keeps one timestamp per retained page across repeated first-page refreshes", async () => {
    const dataSource = new PagedDataSource();
    const coordinator = new RefreshCoordinator();
    render(
      <RunMap
        coordinator={coordinator}
        dataSource={dataSource}
        filters={filters}
        datasetKey="scope-observations"
        selectedRunId={null}
        onSelectRun={vi.fn()}
        theme="light"
      />,
    );
    expect(await screen.findByText("28/28 loaded")).toBeInTheDocument();

    for (let refresh = 0; refresh < 4; refresh += 1) {
      coordinator.refreshAll();
      await waitFor(() => expect(dataSource.firstPageReads).toBe(refresh + 2));
    }

    expect(dataSource.runCalls.filter((call) => call.offset === 15)).toHaveLength(5);
    expect(screen.getByRole("option", { name: /standalone-25 · codex · standalone-25/ }))
      .toBeInTheDocument();
    expect(screen.getByText(/Latest successful run page/)).toBeInTheDocument();
  });

  it("loads more than 1000 exact IDs automatically and continues with each successful limit", async () => {
    const largeRuns = Array.from({ length: 1005 }, (_, index) =>
      ({ ...run(`exact-long-run-${String(index).padStart(4, "0")}`, index), shortName: `Long alias ${String(index).padStart(4, "0")} · stable` }),
    );
    class LargeDataSource extends ImmediateDataSource {
      readonly calls: Array<{ offset: number; limit: number }> = [];

      override async runs(
        requestedFilters: DashboardFilters,
        page: { offset: number; limit: number },
      ): Promise<SourceObservation<DashboardRunsData>> {
        this.calls.push(page);
        const maximum = page.offset >= 500 ? 125 : 250;
        if (page.limit > maximum) {
          throw new DashboardDataSourceError(
            "PAGE_ENVELOPE_TOO_LARGE",
            "The fixture page exceeds the fixed envelope.",
          );
        }
        const items = largeRuns.slice(page.offset, page.offset + page.limit);
        const nextOffset = page.offset + page.limit < largeRuns.length
          ? page.offset + page.limit
          : null;
        return observation({
          observedAt: "2026-01-03T00:00:00Z",
          filters: {
            ...requestedFilters,
            fromUtc: requestedFilters.fromUtc.replace(".000Z", "Z"),
            toUtc: requestedFilters.toUtc.replace(".000Z", "Z"),
            projectName: requestedFilters.projectName ?? null,
          },
          coverage: completeCoverage,
          items,
          page: {
            offset: page.offset,
            limit: page.limit,
            nextOffset,
            hasMore: nextOffset !== null,
          },
          totalCount: largeRuns.length,
        });
      }
    }

    const dataSource = new LargeDataSource();
    render(
      <RunMap
        coordinator={new RefreshCoordinator()}
        dataSource={dataSource}
        filters={filters}
        datasetKey="large-cohort"
        selectedRunId={null}
        onSelectRun={vi.fn()}
        theme="light"
      />,
    );

    expect(await screen.findByText("1005/1005 loaded")).toBeInTheDocument();
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(1006);
    expect(new Set(options.slice(1).map((option) => option.getAttribute("value"))).size).toBe(1005);
    expect(screen.getByRole("option", { name: /exact-long-run-1004/ })).toBeInTheDocument();
    expect(dataSource.calls).toEqual([
      { offset: 0, limit: 1000 },
      { offset: 0, limit: 500 },
      { offset: 0, limit: 250 },
      { offset: 250, limit: 250 },
      { offset: 500, limit: 250 },
      { offset: 500, limit: 125 },
      { offset: 625, limit: 125 },
      { offset: 750, limit: 125 },
      { offset: 875, limit: 125 },
      { offset: 1000, limit: 125 },
    ]);
  });

  it("retains exact IDs through a failed contraction and lets fresh metadata win on expansion", async () => {
    type Mode = "initial" | "failed-contraction" | "single" | "expanded";
    class ReconcilingDataSource extends ImmediateDataSource {
      mode: Mode = "initial";
      readonly heldContinuation = deferred<SourceObservation<DashboardRunsData>>();

      override async runs(
        requestedFilters: DashboardFilters,
        page: { offset: number; limit: number },
      ): Promise<SourceObservation<DashboardRunsData>> {
        const maximum = this.mode === "failed-contraction" || this.mode === "single" ? 1 : 3;
        if (page.limit > maximum) {
          throw new DashboardDataSourceError(
            "PAGE_ENVELOPE_TOO_LARGE",
            "Use the fixture's current page envelope.",
          );
        }
        if (this.mode === "failed-contraction" && page.offset === 1) {
          return this.heldContinuation.promise;
        }
        const labels = this.mode === "initial"
          ? ["Old A", "Old B", "Old C"]
          : this.mode === "single"
            ? ["Single A"]
            : ["Fresh A", "Fresh B", "Fresh C"];
        const cohort = labels.map((shortName, index) => ({
          ...run(`exact-${String.fromCharCode(97 + index)}`, index),
          shortName,
        }));
        const effective = this.mode === "single" ? cohort.slice(0, 1) : cohort;
        const items = effective.slice(page.offset, page.offset + page.limit);
        const nextOffset = page.offset + page.limit < effective.length
          ? page.offset + page.limit
          : null;
        return observation({
          observedAt: "2026-01-03T00:00:00Z",
          filters: {
            ...requestedFilters,
            fromUtc: requestedFilters.fromUtc.replace(".000Z", "Z"),
            toUtc: requestedFilters.toUtc.replace(".000Z", "Z"),
            projectName: requestedFilters.projectName ?? null,
          },
          coverage: completeCoverage,
          items,
          page: {
            offset: page.offset,
            limit: page.limit,
            nextOffset,
            hasMore: nextOffset !== null,
          },
          totalCount: effective.length,
        });
      }
    }

    const dataSource = new ReconcilingDataSource();
    const coordinator = new RefreshCoordinator();
    render(
      <RunMap
        coordinator={coordinator}
        dataSource={dataSource}
        filters={filters}
        datasetKey="reconciliation"
        selectedRunId={null}
        onSelectRun={vi.fn()}
        theme="light"
      />,
    );
    expect(await screen.findByText("3/3 loaded")).toBeInTheDocument();

    dataSource.mode = "failed-contraction";
    coordinator.refreshAll();
    expect(await screen.findByRole("option", { name: /Fresh A · cursor · exact-a/ }))
      .toBeInTheDocument();
    expect(screen.getByText("3/3 loaded")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Old B · codex · exact-b/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Old C · cursor · exact-c/ }))
      .toBeInTheDocument();
    expect(screen.getAllByRole("option").map((option) => option.getAttribute("value")))
      .toEqual(["", "exact-a", "exact-b", "exact-c"]);
    await act(async () => dataSource.heldContinuation.reject(new Error("held continuation failed")));
    expect(await screen.findByRole("alert")).toHaveTextContent("held continuation failed");
    expect(screen.getByText("3/3 loaded")).toBeInTheDocument();

    dataSource.mode = "single";
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry run loading" }));
    expect(await screen.findByText("1/1 loaded")).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /exact-b/ })).not.toBeInTheDocument();

    dataSource.mode = "expanded";
    coordinator.refreshAll();
    expect(await screen.findByText("3/3 loaded")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Fresh A · cursor · exact-a/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Fresh B · codex · exact-b/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Fresh C · cursor · exact-c/ }))
      .toBeInTheDocument();
  });

  it("retains initially incomplete successful pages through a failed automatic rebuild", async () => {
    type Mode = "initial" | "poll" | "retry";
    class PartialHistoryDataSource extends ImmediateDataSource {
      mode: Mode = "initial";
      firstPageReads = 0;
      readonly heldPoll = deferred<SourceObservation<DashboardRunsData>>();

      override async runs(
        requestedFilters: DashboardFilters,
        page: { offset: number; limit: number },
      ): Promise<SourceObservation<DashboardRunsData>> {
        if (page.limit > 1) {
          throw new DashboardDataSourceError(
            "PAGE_ENVELOPE_TOO_LARGE",
            "Use one-row fixture pages.",
          );
        }
        if (page.offset === 0) this.firstPageReads += 1;
        if (this.mode === "initial" && page.offset === 2) {
          throw new Error("initial tail unavailable");
        }
        if (this.mode === "poll" && page.offset === 1) return this.heldPoll.promise;

        const label = this.mode === "retry"
          ? `Retry ${String.fromCharCode(65 + page.offset)}`
          : this.mode === "poll"
            ? "Fresh A"
            : `Old ${String.fromCharCode(65 + page.offset)}`;
        const item = {
          ...run(`partial-${String.fromCharCode(97 + page.offset)}`, page.offset),
          shortName: label,
        };
        const nextOffset = page.offset < 2 ? page.offset + 1 : null;
        return observation({
          observedAt: "2026-01-03T00:00:00Z",
          filters: {
            ...requestedFilters,
            fromUtc: requestedFilters.fromUtc.replace(".000Z", "Z"),
            toUtc: requestedFilters.toUtc.replace(".000Z", "Z"),
            projectName: requestedFilters.projectName ?? null,
          },
          coverage: completeCoverage,
          items: [item],
          page: {
            offset: page.offset,
            limit: page.limit,
            nextOffset,
            hasMore: nextOffset !== null,
          },
          totalCount: 3,
        });
      }
    }

    const runtime = new RunMapTimerRuntime();
    const dataSource = new PartialHistoryDataSource();
    const coordinator = new RefreshCoordinator({ runtime, timeoutMs: 1_000_000 });
    render(
      <RunMap
        coordinator={coordinator}
        dataSource={dataSource}
        filters={filters}
        datasetKey="partial-history"
        selectedRunId={null}
        onSelectRun={vi.fn()}
        theme="light"
      />,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("initial tail unavailable");
    expect(screen.getByText("2/3 loaded")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Old A · cursor · partial-a/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Old B · codex · partial-b/ }))
      .toBeInTheDocument();

    dataSource.mode = "poll";
    await act(async () => runtime.advance(SUMMARY_REFRESH_MS));
    await waitFor(() => expect(dataSource.firstPageReads).toBe(2));
    expect(await screen.findByRole("option", { name: /Fresh A · cursor · partial-a/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Old B · codex · partial-b/ }))
      .toBeInTheDocument();
    expect(screen.getByText("2/3 loaded")).toBeInTheDocument();
    expect(screen.getAllByRole("option").map((option) => option.getAttribute("value")))
      .toEqual(["", "partial-a", "partial-b"]);

    await act(async () => dataSource.heldPoll.reject(new Error("automatic continuation failed")));
    expect(await screen.findByRole("alert")).toHaveTextContent("automatic continuation failed");
    expect(screen.getByText("2/3 loaded")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Fresh A · cursor · partial-a/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Old B · codex · partial-b/ }))
      .toBeInTheDocument();

    dataSource.mode = "retry";
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry run loading" }));
    expect(await screen.findByText("3/3 loaded")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Retry A · cursor · partial-a/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Retry B · codex · partial-b/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Retry C · cursor · partial-c/ }))
      .toBeInTheDocument();
  });

  it("does not halve generic errors and stops a typed oversize retry at one row", async () => {
    class FailingDataSource extends ImmediateDataSource {
      readonly limits: number[] = [];

      constructor(private readonly code: string) {
        super();
      }

      override async runs(
        _filters: DashboardFilters,
        page: { offset: number; limit: number },
      ): Promise<SourceObservation<DashboardRunsData>> {
        this.limits.push(page.limit);
        throw new DashboardDataSourceError(this.code, `terminal ${this.code}`);
      }
    }

    const generic = new FailingDataSource("INVALID_ARGUMENT");
    const first = render(
      <RunMap
        coordinator={new RefreshCoordinator()}
        dataSource={generic}
        filters={filters}
        datasetKey="generic-error"
        selectedRunId={null}
        onSelectRun={vi.fn()}
        theme="light"
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("terminal INVALID_ARGUMENT");
    expect(generic.limits).toEqual([1000]);
    expect(screen.getByRole("button", { name: "Retry run loading" })).toBeInTheDocument();
    first.unmount();

    const oversize = new FailingDataSource("PAGE_ENVELOPE_TOO_LARGE");
    render(
      <RunMap
        coordinator={new RefreshCoordinator()}
        dataSource={oversize}
        filters={filters}
        datasetKey="single-row-oversize"
        selectedRunId={null}
        onSelectRun={vi.fn()}
        theme="light"
      />,
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "terminal PAGE_ENVELOPE_TOO_LARGE",
    );
    expect(oversize.limits).toEqual([1000, 500, 250, 125, 62, 31, 15, 7, 3, 1]);
    expect(screen.getByRole("button", { name: "Retry run loading" })).toBeInTheDocument();
  });

  it("aborts an appended page when the dataset retires and fences its late reply", async () => {
    class RetiringPageDataSource extends ImmediateDataSource {
      readonly pageRequests: Array<{
        request: Deferred<SourceObservation<DashboardRunsData>>;
        signal: AbortSignal;
      }> = [];
      firstPageReads = 0;

      override runs(
        requestedFilters: DashboardFilters,
        page: { offset: number; limit: number },
        signal: AbortSignal,
      ): Promise<SourceObservation<DashboardRunsData>> {
        if (page.offset > 0) {
          const request = deferred<SourceObservation<DashboardRunsData>>();
          this.pageRequests.push({ request, signal });
          return request.promise;
        }
        this.firstPageReads += 1;
        const item = run(this.firstPageReads === 1 ? "initial-first-page" : "current-first-page", 1);
        return Promise.resolve(observation({
          observedAt: "2026-01-03T00:00:00Z",
          filters: {
            ...requestedFilters,
            fromUtc: requestedFilters.fromUtc.replace(".000Z", "Z"),
            toUtc: requestedFilters.toUtc.replace(".000Z", "Z"),
            projectName: requestedFilters.projectName ?? null,
          },
          coverage: completeCoverage,
          items: [item],
          page: {
            offset: 0,
            limit: page.limit,
            nextOffset: page.limit,
            hasMore: true,
          },
          totalCount: 2,
        }));
      }
    }

    const dataSource = new RetiringPageDataSource();
    const coordinator = new RefreshCoordinator();
    const props = {
      coordinator,
      dataSource,
      filters,
      selectedRunId: null,
      onSelectRun: vi.fn(),
      theme: "light" as const,
    };
    const view = render(<RunMap {...props} datasetKey="revision-1" />);
    await waitFor(() => expect(dataSource.pageRequests).toHaveLength(1));
    expect(coordinator.activeCount()).toBe(1);

    view.rerender(<RunMap {...props} datasetKey="revision-2" />);
    expect(dataSource.pageRequests[0].signal.aborted).toBe(true);
    expect(await screen.findByRole("option", { name: /current-first-page · codex/ })).toBeInTheDocument();
    await waitFor(() => expect(dataSource.pageRequests).toHaveLength(2));
    expect(coordinator.activeCount()).toBe(1);

    await act(async () => {
      dataSource.pageRequests[0].request.resolve(observation({
        observedAt: "2026-01-03T00:00:30Z",
        filters: {
          fromUtc: "2026-01-01T00:00:00Z",
          toUtc: "2026-01-03T00:00:00Z",
          timeZone: "UTC",
          projectName: null,
        },
        coverage: completeCoverage,
        items: [run("late-old-page", 2)],
        page: { offset: 1000, limit: 1000, nextOffset: null, hasMore: false },
        totalCount: 2,
      }));
    });
    expect(screen.queryByText("late-old-page")).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: /current-first-page · codex/ })).toBeInTheDocument();

    await act(async () => {
      dataSource.pageRequests[1].request.resolve(observation({
        observedAt: "2026-01-03T00:00:31Z",
        filters: {
          fromUtc: "2026-01-01T00:00:00Z",
          toUtc: "2026-01-03T00:00:00Z",
          timeZone: "UTC",
          projectName: null,
        },
        coverage: completeCoverage,
        items: [run("current-second-page", 2)],
        page: { offset: 1000, limit: 1000, nextOffset: null, hasMore: false },
        totalCount: 2,
      }));
    });
    expect(await screen.findByRole("option", { name: /current-second-page/ })).toBeInTheDocument();
    await waitFor(() => expect(coordinator.activeCount()).toBe(0));
  });

  it("retains a compatible successful cohort when an explicit refresh fails", async () => {
    class FailedRefreshDataSource extends ImmediateDataSource {
      calls = 0;

      override runs(
        requestedFilters: DashboardFilters,
        page: { offset: number; limit: number },
      ): Promise<SourceObservation<DashboardRunsData>> {
        this.calls += 1;
        if (this.calls > 1) return Promise.reject(new Error("refresh unavailable"));
        return Promise.resolve(observation({
          observedAt: "2026-01-03T00:00:00Z",
          filters: {
            ...requestedFilters,
            fromUtc: requestedFilters.fromUtc.replace(".000Z", "Z"),
            toUtc: requestedFilters.toUtc.replace(".000Z", "Z"),
            projectName: requestedFilters.projectName ?? null,
          },
          coverage: completeCoverage,
          items: [run("retained-run", 1)],
          page: { offset: 0, limit: page.limit, nextOffset: null, hasMore: false },
          totalCount: 1,
        }));
      }
    }

    const dataSource = new FailedRefreshDataSource();
    const coordinator = new RefreshCoordinator();
    const props = {
      coordinator,
      dataSource,
      filters,
      selectedRunId: null,
      onSelectRun: vi.fn(),
      theme: "light" as const,
    };
    const view = render(<RunMap {...props} datasetKey="revision-1" />);
    expect(await screen.findByRole("option", { name: /retained-run · codex/ })).toBeInTheDocument();

    view.rerender(<RunMap {...props} datasetKey="revision-2" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "showing 1 distinct IDs from successful pages",
    );
    expect(screen.getByRole("option", { name: /retained-run · codex/ })).toBeInTheDocument();
    expect(screen.getByText("1/1 loaded")).toBeInTheDocument();
  });

  it("clears an incompatible first-page success and never labels a failed scope as empty", async () => {
    class FailedScopeDataSource extends ImmediateDataSource {
      readonly newScope = deferred<SourceObservation<DashboardRunsData>>();
      calls = 0;

      override runs(
        requestedFilters: DashboardFilters,
        page: { offset: number; limit: number },
      ): Promise<SourceObservation<DashboardRunsData>> {
        this.calls += 1;
        if (this.calls > 1) return this.newScope.promise;
        return Promise.resolve(observation({
          observedAt: "2026-01-03T00:00:00Z",
          filters: {
            ...requestedFilters,
            fromUtc: requestedFilters.fromUtc.replace(".000Z", "Z"),
            toUtc: requestedFilters.toUtc.replace(".000Z", "Z"),
            projectName: requestedFilters.projectName ?? null,
          },
          coverage: completeCoverage,
          items: [run("old-scope-run", 1)],
          page: { offset: 0, limit: page.limit, nextOffset: null, hasMore: false },
          totalCount: 1,
        }));
      }
    }

    const dataSource = new FailedScopeDataSource();
    const coordinator = new RefreshCoordinator();
    const props = {
      coordinator,
      dataSource,
      selectedRunId: null,
      onSelectRun: vi.fn(),
      theme: "light" as const,
    };
    const view = render(
      <RunMap {...props} filters={filters} datasetKey="old-scope" />,
    );
    expect(await screen.findByRole("option", { name: /old-scope-run · codex/ })).toBeInTheDocument();

    view.rerender(
      <RunMap
        {...props}
        filters={{ ...filters, projectName: "different-project" }}
        datasetKey="new-scope"
      />,
    );
    expect(screen.queryByText("old-scope-run")).not.toBeInTheDocument();
    await act(async () => dataSource.newScope.reject(new Error("new scope unavailable")));
    expect(screen.getByRole("alert")).toHaveTextContent("new scope unavailable");
    expect(screen.getByText("Run data is unavailable until a successful read.")).toBeInTheDocument();
    expect(screen.queryByText("No started runs match the applied scope.")).not.toBeInTheDocument();
  });

  it("assigns every replacement generation a unique full-step chart coordinate", () => {
    const replacements = Array.from({ length: 30 }, (_, index) =>
      run(`replacement-${index + 1}`, index, {
        ...replacementBinding,
        generation: index + 1,
        sourceRunId: index === 0 ? null : `replacement-${index}`,
      }),
    );
    const option = runMapOption(replacements);
    const series = option.series as Array<{
      data: Array<{ runId: string; value: [number, number] }>;
    }>;
    expect(series[0].data.map((item) => item.value[0])).toEqual(
      Array.from({ length: 30 }, (_, index) => index + 1),
    );
    expect(new Set(series[0].data.map((item) => item.value[0])).size).toBe(30);
  });
});
