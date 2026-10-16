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
import { completeCoverage, ImmediateDataSource, observation } from "./test-fixtures";
import { RefreshCoordinator } from "./refreshCoordinator";

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

function run(
  runId: string,
  index: number,
  binding: DashboardRunItem["sequenceBinding"] = null,
): DashboardRunItem {
  return {
    runId,
    projectName: "project-a",
    implementerProvider: index % 2 ? "codex" : "cursor",
    state: index % 3 ? "completed" : "waiting_codex_capacity",
    submittedAt: `2026-01-01T00:${String(index).padStart(2, "0")}:00Z`,
    firstExecutionLaunchAt: `2026-01-01T01:${String(index).padStart(2, "0")}:00Z`,
    sequenceBinding: binding,
  };
}

const replacementBinding = {
  sequenceId: "sequence-authority",
  sequenceName: "Authoritative sequence",
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
  readonly runCalls: Array<{ filters: DashboardFilters; offset: number }> = [];
  firstPageReads = 0;

  override async runs(
    requestedFilters: DashboardFilters,
    page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>> {
    this.runCalls.push({ filters: requestedFilters, offset: page.offset });
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

describe("C264-1 authoritative paged run map", () => {
  it("keeps replacement generations distinct, exposes total vs loaded, and selects exact IDs", async () => {
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

    expect(await screen.findByText("Loaded 25 of 28 started runs")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^old-replacement cursor/ })).toHaveTextContent(
      "generation 1 · planned_run",
    );
    expect(screen.getByRole("button", { name: /^new-replacement codex/ })).toHaveTextContent(
      "generation 2 · implementer_retry",
    );
    expect(screen.getByText(/Page observations:/)).toHaveTextContent(
      "not one atomic snapshot",
    );
    expect(select).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Load more runs" }));
    expect(await screen.findByText("Loaded 28 of 28 started runs")).toBeInTheDocument();
    expect(dataSource.runCalls.map((call) => call.offset)).toEqual([0, 25]);

    await user.click(screen.getByRole("button", { name: /^new-replacement codex/ }));
    expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ runId: "new-replacement" }));
    await user.click(
      screen.getByRole("button", { name: "Selectable chart cells for authoritative run IDs" }),
    );
    expect(select).toHaveBeenLastCalledWith(expect.objectContaining({ runId: "old-replacement" }));
  });

  it("groups only authoritative bindings and never derives lineage from run names", () => {
    const groups = groupRunMapItems(allRuns.slice(0, 3));
    expect(groups[0].key).toBe("sequence-authority");
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

  it("keeps one timestamp per retained page across repeated first-page refreshes", async () => {
    const user = userEvent.setup();
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
    await user.click(await screen.findByRole("button", { name: "Load more runs" }));
    expect(await screen.findByText("Loaded 28 of 28 started runs")).toBeInTheDocument();

    for (let refresh = 0; refresh < 4; refresh += 1) {
      coordinator.refreshAll();
      await waitFor(() => expect(dataSource.firstPageReads).toBe(refresh + 2));
    }

    expect(dataSource.runCalls.map((call) => call.offset)).toEqual([0, 25, 0, 0, 0, 0]);
    expect(screen.getByRole("button", { name: /^standalone-25 codex/ })).toBeInTheDocument();
    const observations = screen.getByText(/Page observations:/);
    expect(observations).toHaveTextContent("offset 0 · 2026-01-03T00:04:00Z");
    expect(observations).toHaveTextContent("offset 25 · 2026-01-03T00:00:25Z");
    expect(observations).not.toHaveTextContent("offset 0 · 2026-01-03T00:00:00Z");
  });

  it("aborts an appended page when the dataset retires and fences its late reply", async () => {
    class RetiringPageDataSource extends ImmediateDataSource {
      readonly oldPage = deferred<SourceObservation<DashboardRunsData>>();
      pageSignal: AbortSignal | null = null;
      firstPageReads = 0;

      override runs(
        requestedFilters: DashboardFilters,
        page: { offset: number; limit: number },
        signal: AbortSignal,
      ): Promise<SourceObservation<DashboardRunsData>> {
        if (page.offset > 0) {
          this.pageSignal = signal;
          return this.oldPage.promise;
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
          page: { offset: 0, limit: page.limit, nextOffset: 25, hasMore: true },
          totalCount: 2,
        }));
      }
    }

    const user = userEvent.setup();
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
    await user.click(await screen.findByRole("button", { name: "Load more runs" }));
    await waitFor(() => expect(dataSource.pageSignal).not.toBeNull());
    expect(coordinator.activeCount()).toBe(1);

    view.rerender(<RunMap {...props} datasetKey="revision-2" />);
    expect(dataSource.pageSignal?.aborted).toBe(true);
    expect(await screen.findByRole("button", { name: /^current-first-page codex/ })).toBeInTheDocument();
    await waitFor(() => expect(coordinator.activeCount()).toBe(0));

    await act(async () => {
      dataSource.oldPage.resolve(observation({
        observedAt: "2026-01-03T00:00:30Z",
        filters: {
          fromUtc: "2026-01-01T00:00:00Z",
          toUtc: "2026-01-03T00:00:00Z",
          timeZone: "UTC",
          projectName: null,
        },
        coverage: completeCoverage,
        items: [run("late-old-page", 2)],
        page: { offset: 25, limit: 25, nextOffset: null, hasMore: false },
        totalCount: 2,
      }));
    });
    expect(screen.queryByText("late-old-page")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^current-first-page codex/ })).toBeInTheDocument();
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
    expect(await screen.findByRole("button", { name: /^old-scope-run codex/ })).toBeInTheDocument();

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
