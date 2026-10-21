import React from "react";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { TokenAnalytics } from "./TokenAnalytics";
import { RunTokenConsumption } from "./RunTokenConsumption";
import { RefreshCoordinator } from "./refreshCoordinator";
import { ImmediateDataSource, cursorCodexTokenUsage, observation } from "./test-fixtures";
import {
  DashboardDataSourceError,
  type DashboardFilters,
  type DashboardRunItem,
  type SourceObservation,
  type TokenUsageData,
} from "./types";

vi.mock("./DashboardChart", () => ({
  DashboardChart: ({
    ariaLabel,
    onEvents,
    option,
  }: {
    ariaLabel: string;
    onEvents?: Record<string, (value: unknown) => void>;
    option: { series?: Array<{ data?: Array<Record<string, unknown>> }> };
  }) => (
    <button
      type="button"
      aria-label={ariaLabel}
      onClick={() => {
        const node = option.series?.[0]?.data?.find((item) => item.displayLabel === "Cursor · shared");
        onEvents?.click({ data: node });
      }}
    >
      {ariaLabel}
    </button>
  ),
}));

const filters: DashboardFilters = {
  fromUtc: "2026-10-01T00:00:00Z",
  toUtc: "2026-10-02T00:00:00Z",
  timeZone: "America/La_Paz",
};

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

class QueueTokenSource extends ImmediateDataSource {
  hold = false;
  readonly gates: Array<{
    resolve: (value: SourceObservation<TokenUsageData>) => void;
    reject: (error: unknown) => void;
  }> = [];

  override tokenUsage(
    requested: DashboardFilters,
    runId: string | null,
    signal: AbortSignal,
  ): Promise<SourceObservation<TokenUsageData>> {
    this.tokenCalls.push({ filters: requested, runId });
    if (!this.hold) return super.tokenUsage(requested, runId, signal);
    return new Promise((resolve, reject) => {
      this.gates.push({ resolve, reject });
    });
  }
}

class HeldTokenSource extends ImmediateDataSource {
  readonly cohort = deferred<SourceObservation<TokenUsageData>>();
  cohortReads = 0;

  override tokenUsage(
    requested: DashboardFilters,
    runId: string | null,
    signal: AbortSignal,
  ): Promise<SourceObservation<TokenUsageData>> {
    this.tokenCalls.push({ filters: requested, runId });
    if (runId !== null) return super.tokenUsage(requested, runId, signal);
    this.cohortReads += 1;
    if (this.cohortReads === 1) return this.cohort.promise;
    return super.tokenUsage(requested, null, signal);
  }

  override async runs(): Promise<never> {
    throw new Error("global token charts must not page runs");
  }
}

const selectedRun: DashboardRunItem = {
  runId: "run-a",
  shortName: "Same alias",
  projectName: "project-a",
  implementerProvider: "cursor",
  state: "completed",
  submittedAt: "2026-10-01T00:00:00Z",
  firstExecutionLaunchAt: "2026-10-01T00:00:00Z",
  sequenceBinding: null,
};

describe("T292-01 token observation owner", () => {
  it("uses one cohort response, fences a held scope, and stops on Pause", async () => {
    vi.useFakeTimers();
    const dataSource = new HeldTokenSource();
    const coordinator = new RefreshCoordinator();
    const view = render(
      <TokenAnalytics
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        theme="light"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(dataSource.tokenCalls).toEqual([{ filters, runId: null }]);
    const replacement = { ...filters, projectName: "project-b" };
    view.rerender(
      <TokenAnalytics
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-b"
        filters={replacement}
        theme="light"
      />,
    );
    await act(async () => {
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    });
    const stale = cursorCodexTokenUsage(replacement);
    stale.source.diagnostics = ["stale-marker"];
    dataSource.cohort.resolve(observation(stale));
    await act(async () => {
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    });
    expect(screen.queryByText(/stale-marker/)).not.toBeInTheDocument();
    expect(screen.getByText("255")).toBeInTheDocument();
    expect(screen.getByText("19/10/2026 08:05", { exact: false })).toBeInTheDocument();

    const callsBeforePause = dataSource.tokenCalls.length;
    coordinator.setPaused(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(dataSource.tokenCalls).toHaveLength(callsBeforePause);
  });

  it("keeps a same-scope refresh and drops an incompatible held, failed, or late scope", async () => {
    const user = userEvent.setup();
    const dataSource = new QueueTokenSource();
    const coordinator = new RefreshCoordinator();
    const view = render(
      <TokenAnalytics
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        theme="light"
      />,
    );
    expect(await screen.findByText("255")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Token model"), "cursor:shared");
    expect(screen.getByText("140")).toBeInTheDocument();

    dataSource.hold = true;
    view.rerender(
      <TokenAnalytics
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a-refresh"
        filters={filters}
        theme="light"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("140")).toBeInTheDocument();
    expect(screen.getByLabelText("Token model")).toHaveValue("cursor:shared");
    const refresh = dataSource.gates.at(-1);
    if (!refresh) throw new Error("same-scope refresh was not held");
    refresh.reject(new Error("refresh failed"));
    expect(await screen.findByRole("alert")).toHaveTextContent("refresh failed");
    expect(screen.getByText("140")).toBeInTheDocument();

    view.rerender(
      <TokenAnalytics
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a-late"
        filters={filters}
        theme="light"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    const lateGate = dataSource.gates.at(-1);
    if (!lateGate || lateGate === refresh) throw new Error("late scope read was not held");
    view.rerender(
      <TokenAnalytics
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-b"
        filters={{ ...filters, projectName: "project-b" }}
        theme="light"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByText("255")).not.toBeInTheDocument();
    expect(screen.queryByText("140")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Token model")).not.toBeInTheDocument();
    expect(screen.getAllByText("Reading token usage…").length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "Token flow" })).toBeInTheDocument();
    const replacement = dataSource.gates.at(-1);
    if (!replacement || replacement === lateGate) throw new Error("replacement scope read was not held");
    const late = cursorCodexTokenUsage({ ...filters, projectName: "project-b" });
    late.source.diagnostics = ["late-scope"];
    lateGate.resolve(observation(late));
    replacement.reject(new Error("scope read failed"));
    expect(await screen.findByRole("alert")).toHaveTextContent("scope read failed");
    expect(screen.queryByText("late-scope")).not.toBeInTheDocument();
    expect(screen.queryByText("255")).not.toBeInTheDocument();
    expect(screen.queryByText("140")).not.toBeInTheDocument();
  });

  it("keeps an older producer unsupported without inventing zero consumption", async () => {
    const dataSource = new ImmediateDataSource();
    dataSource.tokenUsage = async () => {
      throw new DashboardDataSourceError(
        "INVALID_ARGUMENT",
        "Unsupported dashboard read operation.",
      );
    };
    render(
      <TokenAnalytics
        coordinator={new RefreshCoordinator()}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        theme="light"
      />,
    );
    expect((await screen.findAllByText(/not available from this dashboard service/)).length).toBeGreaterThan(0);
    expect(screen.queryByText("Known total")).not.toBeInTheDocument();
    expect(screen.queryByText(/^0$/)).not.toBeInTheDocument();
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("T292-03 linked token facets", () => {
  it("applies the same model facet from the chart click and the accessible select", async () => {
    const user = userEvent.setup();
    const dataSource = new ImmediateDataSource();
    render(
      <TokenAnalytics
        coordinator={new RefreshCoordinator()}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        theme="light"
      />,
    );
    expect(await screen.findByText("255")).toBeInTheDocument();
    const before = dataSource.tokenCalls.length;
    await user.click(screen.getByRole("button", { name: "Token flow" }));
    expect(screen.getByLabelText("Token model")).toHaveValue("cursor:shared");
    expect(screen.getByText("140")).toBeInTheDocument();
    expect(dataSource.tokenCalls).toHaveLength(before);
    await user.selectOptions(screen.getByLabelText("Token role"), "reviewer");
    expect(screen.getAllByText("No known token consumption for this selection.").length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.getByLabelText("Token model")).toHaveValue("");
    expect(screen.getByText("255")).toBeInTheDocument();
    expect(dataSource.tokenCalls.every((call) => call.runId === null)).toBe(true);
  });
});

class AliasTokenSource extends ImmediateDataSource {
  override async tokenUsage(
    requested: DashboardFilters,
    runId: string | null,
    signal: AbortSignal,
  ) {
    const result = await super.tokenUsage(requested, runId, signal);
    if (result.data.selectedRun && runId) {
      result.data.selectedRun = { ...result.data.selectedRun, shortName: "Same alias", runId };
    }
    return result;
  }
}

describe("T292-04 selected-run consumption", () => {
  it("does not read until a run is selected and replaces that exact run", async () => {
    const user = userEvent.setup();
    const dataSource = new AliasTokenSource();
    const coordinator = new RefreshCoordinator();
    const view = render(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        selectedRun={null}
        theme="light"
      />,
    );
    expect(screen.getByText(/Select a run to see its direct token consumption/)).toBeInTheDocument();
    expect(dataSource.tokenCalls).toHaveLength(0);
    expect(screen.queryByText("Precise values")).not.toBeInTheDocument();
    view.rerender(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        selectedRun={selectedRun}
        theme="light"
      />,
    );
    expect((await screen.findAllByText("Same alias")).length).toBeGreaterThan(0);
    expect(dataSource.tokenCalls).toEqual([{ filters, runId: "run-a" }]);
    expect(screen.getByText("Precise values").closest("details")).not.toHaveAttribute("open");
    const other: DashboardRunItem = { ...selectedRun, runId: "run-b" };
    view.rerender(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        selectedRun={other}
        theme="light"
      />,
    );
    await screen.findAllByText("Same alias");
    expect(dataSource.tokenCalls.map((call) => call.runId)).toEqual(["run-a", "run-b"]);
    await user.click(screen.getByText("Precise values"));
    expect(screen.getByText("Run ID").closest("dl")).toHaveTextContent("run-b");
    expect(screen.queryByText("run-a")).not.toBeInTheDocument();
  });

  it("keeps the chart viewport while a replacement is held, failing, or late", async () => {
    const dataSource = new QueueTokenSource();
    const coordinator = new RefreshCoordinator();
    const view = render(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        selectedRun={selectedRun}
        theme="light"
      />,
    );
    expect(await screen.findByText("140", { selector: "[data-run-token-total]" })).toBeInTheDocument();
    const panel = document.querySelector(".run-token-panel");
    if (!(panel instanceof HTMLElement)) throw new Error("Run token panel is missing.");
    const chartSlot = () => panel.querySelector(".run-token-chart-scroll");
    expect(chartSlot()).toBeInstanceOf(HTMLElement);

    dataSource.hold = true;
    view.rerender(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a-refresh"
        filters={filters}
        selectedRun={selectedRun}
        theme="light"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    const late = dataSource.gates[0];
    if (!late) throw new Error("same-run refresh was not held");
    const other: DashboardRunItem = { ...selectedRun, runId: "run-b", shortName: "Replacement alias" };
    view.rerender(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a-refresh"
        filters={filters}
        selectedRun={other}
        theme="light"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryAllByText("140")).toHaveLength(0);
    expect(screen.queryByText("run-a")).not.toBeInTheDocument();
    expect(chartSlot()).toBeInstanceOf(HTMLElement);
    const replacement = dataSource.gates.at(-1);
    if (!late || !replacement || late === replacement) throw new Error("replacement read was not held separately");
    const latePayload = await new ImmediateDataSource().tokenUsage(filters, "run-a", new AbortController().signal);
    latePayload.data.selectedRun = { runId: "run-a", shortName: "Same alias", projectName: "project-a" };
    late.resolve(latePayload);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryAllByText("140")).toHaveLength(0);
    expect(screen.queryByText("run-a")).not.toBeInTheDocument();
    replacement.reject(new Error("direct read failed"));
    expect(await screen.findByRole("alert")).toHaveTextContent("direct read failed");
    expect(screen.queryAllByText("140")).toHaveLength(0);
    expect(chartSlot()).toBeInstanceOf(HTMLElement);
  });

  it("keeps the chart viewport after a zero-total replacement", async () => {
    const dataSource = new QueueTokenSource();
    const coordinator = new RefreshCoordinator();
    const view = render(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        selectedRun={selectedRun}
        theme="light"
      />,
    );
    expect(await screen.findByText("140", { selector: "[data-run-token-total]" })).toBeInTheDocument();
    dataSource.hold = true;
    const other: DashboardRunItem = { ...selectedRun, runId: "run-b", shortName: "Replacement alias" };
    view.rerender(
      <RunTokenConsumption
        coordinator={coordinator}
        dataSource={dataSource}
        datasetKey="scope-a"
        filters={filters}
        selectedRun={other}
        theme="light"
      />,
    );
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryAllByText("140")).toHaveLength(0);
    const replacement = dataSource.gates.at(-1);
    if (!replacement) throw new Error("replacement read was not held");
    const payload = await new ImmediateDataSource().tokenUsage(filters, "run-b", new AbortController().signal);
    payload.data.selectedRun = { runId: "run-b", shortName: "Replacement alias", projectName: "project-a" };
    payload.data.source = {
      ...payload.data.source,
      knownTotalTokens: 0,
      coverage: "complete",
      flows: payload.data.source.flows.map((flow) => ({ ...flow, knownTotal: 0, coverage: "complete" })),
    };
    const zeroCategory = <T extends { knownTotal: number | null }>(category: T): T => ({
      ...category,
      knownTotal: 0,
      coverage: "complete",
    });
    payload.data.rows = payload.data.rows.map((row) => ({
      ...row,
      knownTotalTokens: 0,
      coverage: "complete",
      flows: row.flows.map((flow) => ({ ...flow, knownTotal: 0, coverage: "complete" })),
      categories: Object.fromEntries(
        Object.entries(row.categories).map(([name, category]) => [name, zeroCategory(category)]),
      ),
      nativeCategories: Object.fromEntries(
        Object.entries(row.nativeCategories).map(([name, category]) => [name, zeroCategory(category)]),
      ),
    }));
    replacement.resolve(payload);
    expect(await screen.findByText("Recorded token total is 0.")).toBeInTheDocument();
    expect(screen.getByText("0", { selector: "[data-run-token-total]" })).toBeInTheDocument();
    expect(screen.getAllByText("Replacement alias").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("140")).toHaveLength(0);
    expect(screen.queryByText("run-a")).not.toBeInTheDocument();
    expect(screen.queryByText("No known disjoint token leaves for this run.")).toBeInTheDocument();
    expect(document.querySelector(".run-token-chart-scroll")).toBeInstanceOf(HTMLElement);
    expect(screen.queryByRole("button", { name: "Direct token consumption for Same alias" })).not.toBeInTheDocument();
  });
});
