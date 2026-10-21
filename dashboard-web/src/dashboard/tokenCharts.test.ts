import { describe, expect, it } from "vitest";

import { HeatmapChart, SankeyChart } from "echarts/charts";
import { GridComponent, VisualMapComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { SVGRenderer } from "echarts/renderers";

import { cursorCodexTokenUsage, mixedTokenUsage } from "./test-fixtures";
import {
  facetsFromSelection,
  formatTokenExact,
  positiveFlowTotal,
  projectTokenUsage,
  reconcileTokenFacets,
  tokenHeatmapOption,
  tokenSankeyHeight,
  tokenSankeyOption,
  tokenSelectionFromChartEvent,
  tokenSunburstOption,
} from "./tokenCharts";
import {
  TOKEN_FLOW_NAMES,
  TOKEN_HEATMAP_COLUMNS,
  type TokenFlow,
  type TokenUsageData,
  type TokenUsageRow,
} from "./types";

HTMLCanvasElement.prototype.getContext = (() => null) as unknown as typeof HTMLCanvasElement.prototype.getContext;

echarts.use([SankeyChart, HeatmapChart, GridComponent, VisualMapComponent, SVGRenderer]);

interface RenderedSeriesData {
  count(): number;
  getItemLayout(index: number): { dy?: number };
  getName(index: number): string;
  get(dimension: unknown, index: number): unknown;
  getDimension(dimension: number): unknown;
}

function renderedSeries(chart: echarts.ECharts, index: number): RenderedSeriesData {
  const model = chart as unknown as {
    getModel(): { getSeriesByIndex(seriesIndex: number): { getData(): RenderedSeriesData } };
  };
  return model.getModel().getSeriesByIndex(index).getData();
}

function sankeyLeafValues(option: ReturnType<typeof tokenSankeyOption>): number[] {
  const series = option.series as Array<{ links: Array<Record<string, unknown>> }>;
  return series[0].links
    .filter((link) => String(link.target).startsWith("leaf"))
    .map((link) => link.value as number);
}

function sunburstLeafValues(option: ReturnType<typeof tokenSunburstOption>): number[] {
  const series = option.series as Array<{ data: Array<{ children?: Array<{ children?: Array<{ value: number }> }> }> }>;
  return series[0].data.flatMap((model) =>
    (model.children ?? []).flatMap((role) => (role.children ?? []).map((leaf) => leaf.value)),
  );
}

function heatmapSeries(
  option: ReturnType<typeof tokenHeatmapOption>,
  name: string,
): Array<Record<string, unknown>> {
  const series = option.series as Array<{ name?: string; data: Array<Record<string, unknown>> }>;
  return series.find((item) => item.name === name)?.data ?? [];
}

function heatmapCells(option: ReturnType<typeof tokenHeatmapOption>): Array<Record<string, unknown>> {
  return heatmapSeries(option, "measured");
}

function visualMaps(option: ReturnType<typeof tokenHeatmapOption>): Array<{
  seriesIndex: number;
  min: number;
  max: number;
}> {
  const value = option.visualMap;
  return (Array.isArray(value) ? value : [value]) as Array<{
    seriesIndex: number;
    min: number;
    max: number;
  }>;
}

function sankeyModel(option: ReturnType<typeof tokenSankeyOption>, label: string): Record<string, unknown> {
  const nodes = (option.series as Array<{ data: Array<Record<string, unknown>> }>)[0].data;
  const node = nodes.find((item) => item.displayLabel === label && String(item.name).startsWith("model"));
  if (!node) throw new Error(`Missing Sankey model ${label}`);
  return node;
}

function sunburstModel(option: ReturnType<typeof tokenSunburstOption>, label: string): Record<string, unknown> {
  const nodes = (option.series as Array<{ data: Array<Record<string, unknown>> }>)[0].data;
  const node = nodes.find((item) => item.displayLabel === label);
  if (!node) throw new Error(`Missing sunburst model ${label}`);
  return node;
}

function unknownRole(row: TokenUsageRow): TokenUsageRow {
  return {
    ...row,
    role: "reviewer",
    stage: "review",
    knownTotalTokens: null,
    coverage: "unknown",
    inferenceAttempts: 1,
    knownAttempts: 0,
    partialAttempts: 0,
    unknownAttempts: 1,
    flows: row.flows.map((flow) => ({
      ...flow,
      knownTotal: null,
      knownObservations: 0,
      coverage: "unknown",
    })),
    categories: Object.fromEntries(
      Object.entries(row.categories).map(([name, category]) => [
        name,
        { ...category, knownTotal: null, knownObservations: 0, coverage: "unknown" as const },
      ]),
    ),
  };
}

function withRows(rows: TokenUsageRow[]): TokenUsageData {
  return { ...cursorCodexTokenUsage(), rows };
}

function fiveLeafRow(model: string): TokenUsageRow {
  const template = cursorCodexTokenUsage().rows.find((row) => row.provider === "cursor");
  if (!template) throw new Error("Cursor fixture row is missing.");
  const positive = new Set(["input_other", "cache_read", "cache_write", "output_other", "reasoning_output"]);
  const flows: TokenFlow[] = TOKEN_FLOW_NAMES.map((name) => ({
    name,
    knownTotal: positive.has(name) ? 10 : null,
    knownObservations: positive.has(name) ? 1 : 0,
    observationCount: 1,
    coverage: positive.has(name) ? "complete" : "unknown",
  }));
  return {
    ...template,
    provider: "cursor",
    configuredModel: model,
    role: "implementer",
    knownTotalTokens: 50,
    coverage: "complete",
    flows,
  };
}

describe("T292-02 token chart arithmetic", () => {
  it("unions multiple provider-qualified models once across all three charts", () => {
    const rows = cursorCodexTokenUsage().rows;
    const data = withRows([...rows, { ...rows[0], configuredModel: "excluded" }]);
    const models = [
      { provider: "cursor" as const, configuredModel: "shared" },
      { provider: "codex" as const, configuredModel: "shared" },
    ];
    const view = projectTokenUsage(data, { models: [...models, models[0]], role: null });
    expect(view.knownTotalTokens).toBe(255);
    expect(view.heatmap.map((row) => row.label)).toEqual(["Codex · shared", "Cursor · shared"]);
    expect(sankeyLeafValues(tokenSankeyOption(view)).reduce((sum, value) => sum + value, 0)).toBe(255);
    expect(sunburstLeafValues(tokenSunburstOption(view)).reduce((sum, value) => sum + value, 0)).toBe(255);
    expect(projectTokenUsage(data, { models, role: "reviewer" }).knownTotalTokens).toBeNull();
  });

  it("toggles chart models, preserves other choices and only drops vanished models on refresh", () => {
    const cursor = { provider: "cursor" as const, configuredModel: "shared" };
    const codex = { provider: "codex" as const, configuredModel: "shared" };
    const current = { models: [cursor, codex], role: "reviewer" as const };
    const toggled = facetsFromSelection({ ...cursor, kind: "model", role: null }, current);
    expect(toggled).toEqual({ models: [codex], role: "reviewer" });
    const role = facetsFromSelection({ ...cursor, kind: "role", role: "implementer" }, current);
    expect(role).toEqual({ models: [cursor, codex], role: "implementer" });
    const reconciled = reconcileTokenFacets(
      { models: [cursor, codex, { ...cursor, configuredModel: "gone" }], role: "implementer" },
      cursorCodexTokenUsage().rows,
    );
    expect(reconciled).toEqual({ models: [cursor, codex], role: "implementer" });
    expect(reconcileTokenFacets(current, [])).toEqual({ models: [], role: null });
  });

  it("counts cache and reasoning once and keeps provider identity", () => {
    const data = cursorCodexTokenUsage();
    const view = projectTokenUsage(data, { models: [], role: null });
    expect(data.source.knownTotalTokens).toBe(255);
    expect(view.knownTotalTokens).toBe(255);
    expect(view.flows.map((item) => [item.name, item.knownTotal])).toEqual([
      ["input_other", 180],
      ["cache_read", 40],
      ["cache_write", 5],
      ["input_unsplit", null],
      ["output_other", 24],
      ["reasoning_output", 6],
      ["output_unsplit", null],
    ]);
    const sankey = tokenSankeyOption(view);
    const sunburst = tokenSunburstOption(view);
    expect(sankeyLeafValues(sankey).reduce((sum, value) => sum + value, 0)).toBe(255);
    expect(sunburstLeafValues(sunburst).reduce((sum, value) => sum + value, 0)).toBe(255);
    expect(positiveFlowTotal(view.flows)).toBe(255);
    expect((sunburst.title as { text: string }).text).toBe("255");

    const labels = (sankey.series as Array<{ data: Array<{ displayLabel: string; name: string }> }>)[0]
      .data.filter((node) => node.name.startsWith("model"))
      .map((node) => node.displayLabel);
    expect(labels).toEqual(["Codex · shared", "Cursor · shared"]);
    const ids = (sankey.series as Array<{ data: Array<{ name: string }> }>)[0]
      .data.filter((node) => node.name.startsWith("model"))
      .map((node) => node.name);
    expect(new Set(ids).size).toBe(2);

    const cursorCells = heatmapCells(tokenHeatmapOption(view))
      .filter((cell) => String(cell.displayLabel).startsWith("Cursor · shared"));
    const cursorSum = cursorCells.reduce((sum, cell) => sum + Number((cell.value as number[])[2]), 0);
    expect(cursorSum).toBe(125 + 100 + 20 + 5 + 15 + 3);
    expect(cursorSum).not.toBe(140);
    const codexCacheWrite = heatmapCells(tokenHeatmapOption(view)).find((cell) =>
      cell.displayLabel === "Codex · shared · Cache write",
    );
    expect((codexCacheWrite?.value as number[])[2]).toBe(7);
    const codexCacheLeaf = sankeyLeafValues(sankey);
    expect(codexCacheLeaf.filter((value) => value === 7)).toEqual([]);

    const serialized = JSON.stringify(sankey) + JSON.stringify(sunburst);
    expect(serialized).not.toContain("NaN");
    expect(serialized).not.toContain("Infinity");
  });

  it("keeps unknown, partial, and recorded zero distinct", () => {
    const view = projectTokenUsage(mixedTokenUsage(), { models: [], role: null });
    expect(view.heatmap.some((row) => row.configuredModel === null)).toBe(true);
    const option = tokenHeatmapOption(view);
    const cells = heatmapCells(option);
    const unknown = heatmapSeries(option, "unmeasured").find((cell) =>
      String(cell.displayLabel).startsWith("Cursor · Unrecorded model"),
    );
    const zero = cells.find((cell) => cell.displayLabel === "Cursor · recorded-zero · Input total");
    const partial = cells.find((cell) => cell.displayLabel === "Cursor · partial-model · Input total");
    expect(unknown?.exact).toBeNull();
    expect(unknown?.unmeasured).toBe(true);
    expect(cells.some((cell) => cell.exact === null)).toBe(false);
    expect(visualMaps(option).map((item) => item.seriesIndex)).toEqual([0, 1]);
    const tooltip = (option.tooltip as { formatter: (value: unknown) => string }).formatter;
    expect(tooltip({ data: unknown })).toContain("Unknown");
    expect(tooltip({ data: unknown })).not.toContain(">0<");
    expect(tooltip({ data: zero })).toContain("0");
    expect(tooltip({ data: zero })).not.toContain("Unknown");
    expect((zero?.value as number[])[2]).toBe(0);
    expect(zero?.itemStyle).toBeUndefined();
    expect(partial?.coverage).toBe("partial");
    expect(visualMaps(option)[0]?.min).toBe(0);
    expect(visualMaps(option)[0]?.max).toBeGreaterThan(0);
    const selected = tokenSelectionFromChartEvent({ data: unknown });
    expect(selected).toMatchObject({
      kind: "model",
      provider: "cursor",
      configuredModel: null,
      role: null,
    });

    const zeroOnly = projectTokenUsage(mixedTokenUsage(), {
      models: [{ provider: "cursor", configuredModel: "recorded-zero" }],
      role: null,
    });
    const zeroOption = tokenHeatmapOption(zeroOnly);
    expect(visualMaps(zeroOption)[0]?.max).toBe(0);
    expect(heatmapCells(zeroOption).every((cell) => (cell.value as number[])[2] === 0 || (cell.value as Array<number | null>)[2] === null)).toBe(true);
  });

  it("escapes model text and omits technical ids from tooltips", () => {
    const view = projectTokenUsage(mixedTokenUsage(), { models: [], role: null });
    const option = tokenSankeyOption(view);
    const formatter = (option.tooltip as { formatter: (value: unknown) => string }).formatter;
    const malicious = (option.series as Array<{ data: Array<Record<string, unknown>> }>)[0].data.find((node) =>
      String(node.displayLabel).includes("<img"),
    );
    expect(malicious).toBeDefined();
    const html = formatter({ data: malicious });
    expect(html).toContain("&lt;img");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("runId");
    expect(html).not.toContain("attempt");
    expect(formatTokenExact(140)).toBe("140");
  });

  it("selects provider-qualified model and role without treating a heatmap column as a leaf", () => {
    const data = cursorCodexTokenUsage();
    const sankey = tokenSankeyOption(projectTokenUsage(data, { models: [], role: null }));
    const cursor = (sankey.series as Array<{ data: Array<Record<string, unknown>> }>)[0].data.find((node) =>
      node.displayLabel === "Cursor · shared" && String(node.name).startsWith("model"),
    );
    const selected = tokenSelectionFromChartEvent({ data: cursor });
    expect(selected).toEqual({
      kind: "model",
      provider: "cursor",
      configuredModel: "shared",
      role: null,
    });
    expect(facetsFromSelection(selected!).role).toBeNull();
    const filtered = projectTokenUsage(data, facetsFromSelection(selected!));
    expect(filtered.knownTotalTokens).toBe(140);
    expect(filtered.heatmap.map((row) => row.label)).toEqual(["Cursor · shared"]);

    const heatmap = tokenHeatmapOption(projectTokenUsage(data, { models: [], role: null }));
    const cell = heatmapCells(heatmap).find((item) => item.displayLabel === "Codex · shared · Cache write");
    const fromCell = tokenSelectionFromChartEvent({ data: cell });
    expect(fromCell).toMatchObject({
      kind: "model",
      provider: "codex",
      configuredModel: "shared",
      role: null,
    });
    expect(TOKEN_HEATMAP_COLUMNS).not.toContain("cache_write_input_tokens");
    expect(reconcileTokenFacets(
      { models: [{ provider: "cursor", configuredModel: "missing" }], role: "reviewer" },
      data.rows,
    )).toEqual({ models: [], role: null });
  });

  it("aggregates a Sankey model across both roles, including an unknown role", () => {
    const cursor = cursorCodexTokenUsage().rows.find((row) => row.provider === "cursor");
    if (!cursor) throw new Error("Cursor fixture row is missing.");
    const both = withRows([cursor, { ...cursor, role: "reviewer", stage: "review" }]);
    const bothView = projectTokenUsage(both, { models: [], role: null });
    const filtered = projectTokenUsage(both, {
      models: [{ provider: "cursor", configuredModel: "shared" }],
      role: null,
    });
    const sankey = sankeyModel(tokenSankeyOption(bothView), "Cursor · shared");
    const sunburst = sunburstModel(tokenSunburstOption(bothView), "Cursor · shared");
    expect(filtered.knownTotalTokens).toBe(280);
    expect(filtered.coverage).toBe("complete");
    expect(sankey.exact).toBe(280);
    expect(sankey.coverage).toBe("complete");
    expect(sunburst.exact).toBe(filtered.knownTotalTokens);
    expect(sunburst.coverage).toBe(filtered.coverage);

    const mixed = withRows([cursor, unknownRole(cursor)]);
    const mixedView = projectTokenUsage(mixed, { models: [], role: null });
    const mixedFiltered = projectTokenUsage(mixed, {
      models: [{ provider: "cursor", configuredModel: "shared" }],
      role: null,
    });
    const mixedSankey = sankeyModel(tokenSankeyOption(mixedView), "Cursor · shared");
    const mixedSunburst = sunburstModel(tokenSunburstOption(mixedView), "Cursor · shared");
    expect(mixedFiltered.knownTotalTokens).toBe(140);
    expect(mixedFiltered.coverage).toBe("partial");
    expect(mixedSankey.exact).toBe(140);
    expect(mixedSankey.coverage).toBe("partial");
    expect(mixedSunburst.exact).toBe(mixedFiltered.knownTotalTokens);
    expect(mixedSunburst.coverage).toBe(mixedFiltered.coverage);
  });

  it("gives every Sankey node a positive height when eight models have five leaves", () => {
    const rows = Array.from({ length: 8 }, (_, index) => fiveLeafRow(`model-${index}`));
    const view = projectTokenUsage(withRows(rows), { models: [], role: null });
    const height = tokenSankeyHeight(view);
    expect(height).toBeGreaterThan(480);
    const option = tokenSankeyOption(view);
    const host = document.createElement("div");
    host.style.width = "960px";
    host.style.height = `${height}px`;
    document.body.appendChild(host);
    const chart = echarts.init(host, undefined, { renderer: "svg", width: 960, height });
    chart.setOption(option);
    const data = renderedSeries(chart, 0);
    const names = new Set<string>();
    expect(data.count()).toBeGreaterThan(8 * 5);
    for (let index = 0; index < data.count(); index += 1) {
      const layout = data.getItemLayout(index) as { dy?: number };
      expect(layout.dy).toBeGreaterThan(0);
      const name = String(data.getName(index) ?? "");
      expect(name.length).toBeGreaterThan(0);
      expect(names.has(name)).toBe(false);
      names.add(name);
    }
    const narrow = document.createElement("div");
    narrow.style.width = "640px";
    narrow.style.height = `${height}px`;
    document.body.appendChild(narrow);
    const narrowChart = echarts.init(narrow, undefined, { renderer: "svg", width: 640, height });
    narrowChart.setOption(option);
    const narrowData = renderedSeries(narrowChart, 0);
    for (let index = 0; index < narrowData.count(); index += 1) {
      const layout = narrowData.getItemLayout(index) as { dy?: number };
      expect(layout.dy).toBeGreaterThan(0);
    }
    chart.dispose();
    narrowChart.dispose();
    host.remove();
    narrow.remove();
  });

  it("renders unknown heatmap cells apart from measured zeros", () => {
    const view = projectTokenUsage(mixedTokenUsage(), { models: [], role: null });
    const option = tokenHeatmapOption(view);
    const height = 520;
    const host = document.createElement("div");
    document.body.appendChild(host);
    const chart = echarts.init(host, undefined, { renderer: "svg", width: 880, height });
    chart.setOption(option);
    const svg = host.innerHTML;
    expect(svg).toContain('fill="rgb(217,211,197)"');
    expect(svg).toContain('fill="rgb(231,238,246)"');
    const measured = renderedSeries(chart, 0);
    for (let index = 0; index < measured.count(); index += 1) {
      expect(Number.isFinite(measured.get(measured.getDimension(2), index))).toBe(true);
    }
    const unmeasured = renderedSeries(chart, 1);
    expect(unmeasured.count()).toBeGreaterThan(0);
    chart.dispose();
    host.remove();
  });
});
