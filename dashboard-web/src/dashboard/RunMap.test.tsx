import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  DashboardFilters,
  DashboardRunItem,
  DashboardRunsData,
  SourceObservation,
} from "./types";
import { completeCoverage, ImmediateDataSource, observation } from "./test-fixtures";

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

  override async runs(
    requestedFilters: DashboardFilters,
    page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>> {
    this.runCalls.push({ filters: requestedFilters, offset: page.offset });
    const items = allRuns.slice(page.offset, page.offset + page.limit);
    const nextOffset = page.offset + page.limit < allRuns.length
      ? page.offset + page.limit
      : null;
    return observation({
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
  }
}

describe("C264-1 authoritative paged run map", () => {
  it("keeps replacement generations distinct, exposes total vs loaded, and selects exact IDs", async () => {
    const user = userEvent.setup();
    const dataSource = new PagedDataSource();
    const select = vi.fn();
    render(
      <RunMap
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
