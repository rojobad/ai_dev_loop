import type { EChartsCoreOption } from "echarts/core";

import { escapeHtml } from "./charts";
import {
  DashboardDataSourceError,
  TOKEN_FLOW_NAMES,
  TOKEN_HEATMAP_COLUMNS,
  type TokenCoverage,
  type TokenFlow,
  type TokenFlowName,
  type TokenHeatmapColumn,
  type TokenProviderName,
  type TokenRoleName,
  type TokenUsageData,
  type TokenUsageRow,
} from "./types";

export const TOKEN_FLOW_COLORS: Record<TokenFlowName, string> = {
  input_other: "#2f76d2",
  cache_read: "#176f62",
  cache_write: "#4bb6a2",
  input_unsplit: "#8054ae",
  output_other: "#b8780b",
  reasoning_output: "#c64e64",
  output_unsplit: "#727984",
};

const UNKNOWN_CELL_COLOR = "#d9d3c5";
const MEASURED_ZERO_COLOR = "#e7eef6";
const MEASURED_HIGH_COLOR = "#176f62";

export interface TokenModelFacet {
  provider: TokenProviderName;
  configuredModel: string | null;
}

export interface TokenFacets {
  models: TokenModelFacet[];
  role: TokenRoleName | null;
}

export const EMPTY_TOKEN_FACETS: TokenFacets = { models: [], role: null };

export interface TokenSelectionTarget {
  kind: "model" | "role" | "leaf";
  provider: TokenProviderName;
  configuredModel: string | null;
  role: TokenRoleName | null;
}

export interface TokenFlowAmount {
  name: TokenFlowName;
  knownTotal: number | null;
  coverage: TokenCoverage;
}

export interface TokenRoleBucket {
  provider: TokenProviderName;
  configuredModel: string | null;
  role: TokenRoleName;
  knownTotalTokens: number | null;
  coverage: TokenCoverage;
  flows: TokenFlowAmount[];
}

export interface TokenHeatmapCell {
  column: TokenHeatmapColumn;
  knownTotal: number | null;
  coverage: TokenCoverage;
}

export interface TokenHeatmapRow {
  provider: TokenProviderName;
  configuredModel: string | null;
  label: string;
  cells: TokenHeatmapCell[];
}

export interface TokenUsageView {
  filtered: boolean;
  knownTotalTokens: number | null;
  coverage: TokenCoverage;
  inferenceAttempts: number;
  knownAttempts: number;
  partialAttempts: number;
  unknownAttempts: number;
  flows: TokenFlowAmount[];
  buckets: TokenRoleBucket[];
  heatmap: TokenHeatmapRow[];
  diagnostics: string[];
}

interface ChartDatum {
  displayLabel: string;
  tokenSelection: TokenSelectionTarget;
  exact: number | null;
  coverage: TokenCoverage;
}

function addTokens(values: number[]): number {
  return values.reduce((sum, value) => {
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new TypeError("token amount is not a safe integer.");
    }
    const next = sum + value;
    if (!Number.isSafeInteger(next)) {
      throw new TypeError("token amount is not a safe integer.");
    }
    return next;
  }, 0);
}

function coverageOf(parts: TokenCoverage[], knownCount: number, totalCount: number): TokenCoverage {
  if (totalCount === 0) return "not_applicable";
  if (knownCount === 0) {
    return parts.every((item) => item === "not_applicable") ? "not_applicable" : "unknown";
  }
  if (knownCount === totalCount && parts.every((item) => item === "complete" || item === "not_applicable")) {
    return "complete";
  }
  return "partial";
}

function sumNullable(values: Array<number | null>, coverages: TokenCoverage[]): {
  knownTotal: number | null;
  coverage: TokenCoverage;
} {
  const known = values.filter((value): value is number => value !== null);
  if (known.length === 0) {
    return { knownTotal: null, coverage: coverageOf(coverages, 0, values.length) };
  }
  return {
    knownTotal: addTokens(known),
    coverage: coverageOf(coverages, known.length, values.length),
  };
}

export function providerLabel(provider: TokenProviderName): string {
  return provider === "cursor" ? "Cursor" : "Codex";
}

export function roleLabel(role: TokenRoleName): string {
  return role === "implementer" ? "Implementer" : "Reviewer";
}

export function stageLabel(stage: TokenUsageRow["stage"]): string {
  if (stage === "initial") return "Initial";
  if (stage === "correction") return "Correction";
  return "Review";
}

export function configuredModelLabel(
  provider: TokenProviderName,
  configuredModel: string | null,
): string {
  return `${providerLabel(provider)} · ${configuredModel ?? "Unrecorded model"}`;
}

export function flowLabel(name: TokenFlowName): string {
  const labels: Record<TokenFlowName, string> = {
    input_other: "Uncached input",
    cache_read: "Cache read",
    cache_write: "Cache write",
    input_unsplit: "Unsplit input",
    output_other: "Other output",
    reasoning_output: "Reasoning output",
    output_unsplit: "Unsplit output",
  };
  return labels[name];
}

export function heatmapColumnLabel(column: TokenHeatmapColumn): string {
  const labels: Record<TokenHeatmapColumn, string> = {
    input_total: "Input total",
    input_uncached: "Input uncached",
    cache_read: "Cache read",
    cache_write: "Cache write",
    output_total: "Output total",
    reasoning_output: "Reasoning output",
  };
  return labels[column];
}

export function coverageLabel(coverage: TokenCoverage): string {
  if (coverage === "complete") return "Complete";
  if (coverage === "partial") return "Partial lower bound";
  if (coverage === "unknown") return "Unknown";
  return "Not applicable";
}

export function formatTokenExact(value: number | null): string {
  if (value === null) return "Unknown";
  if (!Number.isSafeInteger(value)) return "Unknown";
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatTokenTick(value: number): string {
  if (!Number.isSafeInteger(value)) return "";
  const absolute = Math.abs(value);
  if (absolute < 1000) return String(value);
  if (absolute < 1_000_000) {
    const scaled = value / 1000;
    return `${Number.isInteger(scaled) ? scaled : scaled.toFixed(1)}k`;
  }
  const scaled = value / 1_000_000;
  return `${Number.isInteger(scaled) ? scaled : scaled.toFixed(1)}M`;
}

export function modelFacetKey(facet: TokenModelFacet): string {
  return `${facet.provider}:${facet.configuredModel ?? ""}`;
}

export function modelFacetFromKey(value: string): TokenModelFacet | null {
  const match = /^(cursor|codex):(.*)$/.exec(value);
  if (!match) return null;
  return {
    provider: match[1] as TokenProviderName,
    configuredModel: match[2] === "" ? null : match[2],
  };
}

function sameModel(row: TokenUsageRow, facet: TokenModelFacet): boolean {
  return row.provider === facet.provider && row.configuredModel === facet.configuredModel;
}

export function filterTokenRows(rows: TokenUsageRow[], facets: TokenFacets): TokenUsageRow[] {
  return rows.filter((row) => {
    if (facets.models.length && !facets.models.some((model) => sameModel(row, model))) return false;
    if (facets.role && row.role !== facets.role) return false;
    return true;
  });
}

export function reconcileTokenFacets(facets: TokenFacets, rows: TokenUsageRow[]): TokenFacets {
  const models = facets.models.filter((model) => rows.some((row) => sameModel(row, model)));
  if (facets.models.length && !models.length) return EMPTY_TOKEN_FACETS;
  const modelRows = models.length ? rows.filter((row) => models.some((model) => sameModel(row, model))) : rows;
  const role = facets.role && !modelRows.some((row) => row.role === facets.role) ? null : facets.role;
  return models.length === facets.models.length && role === facets.role ? facets : { models, role };
}

function flowAmount(rows: TokenUsageRow[], name: TokenFlowName): TokenFlowAmount {
  const facts = rows.map((row) => {
    const found = row.flows.find((item) => item.name === name);
    if (!found) throw new TypeError("token flow leaf is missing.");
    return found;
  });
  const summed = sumNullable(
    facts.map((item) => item.knownTotal),
    facts.map((item) => item.coverage),
  );
  return { name, knownTotal: summed.knownTotal, coverage: summed.coverage };
}

function bucketRows(rows: TokenUsageRow[]): TokenRoleBucket[] {
  const groups = new Map<string, TokenUsageRow[]>();
  for (const row of rows) {
    const key = `${row.provider}\u0000${row.configuredModel ?? ""}\u0000${row.role}`;
    const current = groups.get(key);
    if (current) current.push(row);
    else groups.set(key, [row]);
  }
  return [...groups.values()].map((group) => {
    const first = group[0];
    const totals = sumNullable(
      group.map((row) => row.knownTotalTokens),
      group.map((row) => row.coverage),
    );
    return {
      provider: first.provider,
      configuredModel: first.configuredModel,
      role: first.role,
      knownTotalTokens: totals.knownTotal,
      coverage: totals.coverage,
      flows: TOKEN_FLOW_NAMES.map((name) => flowAmount(group, name)),
    };
  });
}

function heatmapRows(rows: TokenUsageRow[]): TokenHeatmapRow[] {
  const groups = new Map<string, TokenUsageRow[]>();
  for (const row of rows) {
    const key = modelFacetKey({ provider: row.provider, configuredModel: row.configuredModel });
    const current = groups.get(key);
    if (current) current.push(row);
    else groups.set(key, [row]);
  }
  return [...groups.entries()].map(([, group]) => {
    const first = group[0];
    return {
      provider: first.provider,
      configuredModel: first.configuredModel,
      label: configuredModelLabel(first.provider, first.configuredModel),
      cells: TOKEN_HEATMAP_COLUMNS.map((column) => {
        const facts = group.map((row) => row.categories[column] ?? {
          knownTotal: null,
          knownObservations: 0,
          observationCount: 0,
          coverage: "unknown" as const,
        });
        const summed = sumNullable(
          facts.map((item) => item.knownTotal),
          facts.map((item) => item.coverage),
        );
        return { column, knownTotal: summed.knownTotal, coverage: summed.coverage };
      }),
    };
  });
}

function sourceFlows(flows: TokenFlow[]): TokenFlowAmount[] {
  return TOKEN_FLOW_NAMES.map((name) => {
    const found = flows.find((item) => item.name === name);
    if (!found) throw new TypeError("token flow leaf is missing.");
    return { name, knownTotal: found.knownTotal, coverage: found.coverage };
  });
}

export function projectTokenUsage(data: TokenUsageData, facets: TokenFacets): TokenUsageView {
  const filteredRows = filterTokenRows(data.rows, facets);
  const filtered = facets.models.length > 0 || facets.role !== null;
  const buckets = bucketRows(filteredRows);
  const heatmap = heatmapRows(filteredRows);
  if (!filtered) {
    return {
      filtered: false,
      knownTotalTokens: data.source.knownTotalTokens,
      coverage: data.source.coverage,
      inferenceAttempts: data.source.inferenceAttempts,
      knownAttempts: data.source.knownAttempts,
      partialAttempts: data.source.partialAttempts,
      unknownAttempts: data.source.unknownAttempts,
      flows: sourceFlows(data.source.flows),
      buckets,
      heatmap,
      diagnostics: data.source.diagnostics,
    };
  }
  const totals = sumNullable(
    filteredRows.map((row) => row.knownTotalTokens),
    filteredRows.map((row) => row.coverage),
  );
  const attempts = filteredRows.reduce(
    (sum, row) => ({
      inferenceAttempts: sum.inferenceAttempts + row.inferenceAttempts,
      knownAttempts: sum.knownAttempts + row.knownAttempts,
      partialAttempts: sum.partialAttempts + row.partialAttempts,
      unknownAttempts: sum.unknownAttempts + row.unknownAttempts,
    }),
    { inferenceAttempts: 0, knownAttempts: 0, partialAttempts: 0, unknownAttempts: 0 },
  );
  return {
    filtered: true,
    knownTotalTokens: totals.knownTotal,
    coverage: filteredRows.length === 0 ? "not_applicable" : totals.coverage,
    ...attempts,
    flows: TOKEN_FLOW_NAMES.map((name) => flowAmount(filteredRows, name)),
    buckets,
    heatmap,
    diagnostics: data.source.diagnostics,
  };
}

export function positiveFlowTotal(flows: TokenFlowAmount[]): number {
  return addTokens(
    flows.flatMap((item) => (item.knownTotal !== null && item.knownTotal > 0 ? [item.knownTotal] : [])),
  );
}

function selection(
  kind: TokenSelectionTarget["kind"],
  bucket: Pick<TokenRoleBucket, "provider" | "configuredModel" | "role">,
): TokenSelectionTarget {
  return {
    kind,
    provider: bucket.provider,
    configuredModel: bucket.configuredModel,
    role: kind === "model" ? null : bucket.role,
  };
}

function nodeId(prefix: string, parts: string[]): string {
  return [prefix, ...parts].join("\u001f");
}

function tooltipHtml(label: string, exact: number | null, coverage: TokenCoverage): string {
  return `<strong>${escapeHtml(label)}</strong><br>${escapeHtml(formatTokenExact(exact))} · ${escapeHtml(coverageLabel(coverage))}`;
}

function bucketsByModel(buckets: TokenRoleBucket[]): Map<string, TokenRoleBucket[]> {
  const groups = new Map<string, TokenRoleBucket[]>();
  for (const bucket of buckets) {
    const key = modelFacetKey(bucket);
    const current = groups.get(key);
    if (current) current.push(bucket);
    else groups.set(key, [bucket]);
  }
  return groups;
}

function modelSummary(buckets: TokenRoleBucket[]): { knownTotal: number | null; coverage: TokenCoverage } {
  return sumNullable(
    buckets.map((bucket) => bucket.knownTotalTokens),
    buckets.map((bucket) => bucket.coverage),
  );
}

const TOKEN_SANKEY_NODE_GAP = 8;
const TOKEN_SANKEY_MIN_NODE_PX = 18;
const TOKEN_SANKEY_CHROME_PX = 24;

export function tokenSankeyHeight(view: TokenUsageView): number {
  const seenModels = new Set<string>();
  let models = 0;
  let roles = 0;
  let leaves = 0;
  for (const bucket of view.buckets) {
    const positive = bucket.flows.filter(
      (item) => item.knownTotal !== null && item.knownTotal > 0,
    );
    if (positive.length === 0) continue;
    const key = modelFacetKey(bucket);
    if (!seenModels.has(key)) {
      seenModels.add(key);
      models += 1;
    }
    roles += 1;
    leaves += positive.length;
  }
  const busiest = Math.max(models, roles, leaves, 1);
  const content = busiest * TOKEN_SANKEY_MIN_NODE_PX + (busiest - 1) * TOKEN_SANKEY_NODE_GAP;
  return Math.max(480, TOKEN_SANKEY_CHROME_PX + content);
}

function assertChartNumber(value: number): number {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError("token chart series contains a non-finite amount.");
  }
  return value;
}

export function tokenSankeyOption(view: TokenUsageView): EChartsCoreOption {
  const nodes: Array<ChartDatum & { name: string; depth: number; itemStyle: { color: string } }> = [];
  const links: Array<{
    source: string;
    target: string;
    value: number;
    displayLabel: string;
    exact: number;
    coverage: TokenCoverage;
    tokenSelection: TokenSelectionTarget;
  }> = [];
  const seen = new Set<string>();
  const pushNode = (id: string, datum: ChartDatum & { depth: number; color: string }) => {
    if (seen.has(id)) return;
    seen.add(id);
    nodes.push({
      name: id,
      depth: datum.depth,
      displayLabel: datum.displayLabel,
      tokenSelection: datum.tokenSelection,
      exact: datum.exact,
      coverage: datum.coverage,
      itemStyle: { color: datum.color },
    });
  };
  const models = bucketsByModel(view.buckets);
  for (const bucket of view.buckets) {
    const modelId = nodeId("model", [bucket.provider, bucket.configuredModel ?? ""]);
    const roleId = nodeId("role", [bucket.provider, bucket.configuredModel ?? "", bucket.role]);
    const modelLabel = configuredModelLabel(bucket.provider, bucket.configuredModel);
    const roleText = `${modelLabel} · ${roleLabel(bucket.role)}`;
    const positiveLeaves = bucket.flows.filter(
      (item): item is TokenFlowAmount & { knownTotal: number } =>
        item.knownTotal !== null && item.knownTotal > 0,
    );
    if (positiveLeaves.length === 0) continue;
    const summary = modelSummary(models.get(modelFacetKey(bucket)) ?? [bucket]);
    const roleWidth = assertChartNumber(addTokens(positiveLeaves.map((item) => item.knownTotal)));
    pushNode(modelId, {
      depth: 0,
      color: "#8ea0b8",
      displayLabel: modelLabel,
      exact: summary.knownTotal,
      coverage: summary.coverage,
      tokenSelection: selection("model", bucket),
    });
    pushNode(roleId, {
      depth: 1,
      color: "#52657f",
      displayLabel: roleText,
      exact: bucket.knownTotalTokens,
      coverage: bucket.coverage,
      tokenSelection: selection("role", bucket),
    });
    links.push({
      source: modelId,
      target: roleId,
      value: roleWidth,
      displayLabel: `${modelLabel} → ${roleLabel(bucket.role)}`,
      exact: roleWidth,
      coverage: bucket.coverage,
      tokenSelection: selection("role", bucket),
    });
    for (const leaf of positiveLeaves) {
      const leafId = nodeId("leaf", [
        bucket.provider,
        bucket.configuredModel ?? "",
        bucket.role,
        leaf.name,
      ]);
      const leafText = `${roleText} · ${flowLabel(leaf.name)}`;
      pushNode(leafId, {
        depth: 2,
        color: TOKEN_FLOW_COLORS[leaf.name],
        displayLabel: flowLabel(leaf.name),
        exact: leaf.knownTotal,
        coverage: leaf.coverage,
        tokenSelection: selection("leaf", bucket),
      });
      links.push({
        source: roleId,
        target: leafId,
        value: assertChartNumber(leaf.knownTotal),
        displayLabel: leafText,
        exact: leaf.knownTotal,
        coverage: leaf.coverage,
        tokenSelection: selection("leaf", bucket),
      });
    }
  }
  return {
    animation: false,
    series: [
      {
        type: "sankey",
        data: nodes,
        links,
        nodeAlign: "left",
        nodeGap: TOKEN_SANKEY_NODE_GAP,
        nodeWidth: 16,
        left: 8,
        right: 120,
        top: 12,
        bottom: 12,
        draggable: false,
        emphasis: { focus: "adjacency" },
        label: {
          fontSize: 12,
          formatter: (value: unknown) => chartLabel(value),
        },
        lineStyle: { color: "gradient", opacity: 0.35 },
      },
    ],
    tooltip: {
      renderMode: "html",
      formatter: (value: unknown) => chartTooltip(value),
    },
  };
}

export function tokenSunburstOption(view: TokenUsageView): EChartsCoreOption {
  const models = new Map<string, {
    label: string;
    bucket: TokenRoleBucket;
    roles: TokenRoleBucket[];
  }>();
  for (const bucket of view.buckets) {
    const key = modelFacetKey(bucket);
    const current = models.get(key);
    if (current) current.roles.push(bucket);
    else {
      models.set(key, {
        label: configuredModelLabel(bucket.provider, bucket.configuredModel),
        bucket,
        roles: [bucket],
      });
    }
  }
  const data = [...models.values()].flatMap((model) => {
    const roles = model.roles.flatMap((bucket) => {
      const leaves = bucket.flows.flatMap((leaf) => {
        if (leaf.knownTotal === null || leaf.knownTotal <= 0) return [];
        return [{
          name: flowLabel(leaf.name),
          value: assertChartNumber(leaf.knownTotal),
          displayLabel: `${configuredModelLabel(bucket.provider, bucket.configuredModel)} · ${roleLabel(bucket.role)} · ${flowLabel(leaf.name)}`,
          exact: leaf.knownTotal,
          coverage: leaf.coverage,
          tokenSelection: selection("leaf", bucket),
          itemStyle: { color: TOKEN_FLOW_COLORS[leaf.name] },
        }];
      });
      if (leaves.length === 0) return [];
      const roleValue = assertChartNumber(addTokens(leaves.map((item) => item.value)));
      return [{
        name: roleLabel(bucket.role),
        value: roleValue,
        displayLabel: `${configuredModelLabel(bucket.provider, bucket.configuredModel)} · ${roleLabel(bucket.role)}`,
        exact: bucket.knownTotalTokens,
        coverage: bucket.coverage,
        tokenSelection: selection("role", bucket),
        itemStyle: { color: "#52657f" },
        children: leaves,
      }];
    });
    if (roles.length === 0) return [];
    const modelValue = assertChartNumber(addTokens(roles.map((item) => item.value)));
    const summary = modelSummary(model.roles);
    return [{
      name: model.label,
      value: modelValue,
      displayLabel: model.label,
      exact: summary.knownTotal ?? modelValue,
      coverage: summary.coverage,
      tokenSelection: selection("model", model.bucket),
      itemStyle: { color: "#8ea0b8" },
      children: roles,
    }];
  });
  return {
    animation: false,
    title: {
      text: formatTokenExact(view.knownTotalTokens),
      subtext: "Known total",
      left: "center",
      top: "center",
      textStyle: { fontSize: 16, fontWeight: 650 },
      subtextStyle: { fontSize: 11 },
    },
    series: [
      {
        type: "sunburst",
        data,
        radius: ["22%", "92%"],
        center: ["50%", "52%"],
        sort: undefined,
        nodeClick: false,
        label: {
          fontSize: 12,
          minAngle: 12,
          formatter: (value: unknown) => chartLabel(value),
        },
      },
    ],
    tooltip: {
      renderMode: "html",
      formatter: (value: unknown) => chartTooltip(value),
    },
  };
}

export function tokenHeatmapOption(view: TokenUsageView): EChartsCoreOption {
  const measured = view.heatmap.flatMap((row) =>
    row.cells.flatMap((cell) => (cell.knownTotal === null ? [] : [cell.knownTotal])),
  );
  const measuredMax = measured.length === 0 ? 0 : Math.max(...measured.map(assertChartNumber));
  const cellSelection = (row: TokenHeatmapRow) => ({
    kind: "model" as const,
    provider: row.provider,
    configuredModel: row.configuredModel,
    role: null,
  });
  const measuredData = view.heatmap.flatMap((row, rowIndex) =>
    row.cells.flatMap((cell, columnIndex) => {
      if (cell.knownTotal === null) return [];
      return [{
        value: [columnIndex, rowIndex, assertChartNumber(cell.knownTotal)],
        displayLabel: `${row.label} · ${heatmapColumnLabel(cell.column)}`,
        exact: cell.knownTotal,
        coverage: cell.coverage,
        tokenSelection: cellSelection(row),
        itemStyle: cell.coverage === "partial"
          ? { borderColor: "#b8780b", borderWidth: 2 }
          : undefined,
      }];
    }),
  );
  const unmeasuredData = view.heatmap.flatMap((row, rowIndex) =>
    row.cells.flatMap((cell, columnIndex) => {
      if (cell.knownTotal !== null) return [];
      return [{
        value: [columnIndex, rowIndex, 0],
        displayLabel: `${row.label} · ${heatmapColumnLabel(cell.column)}`,
        exact: null,
        coverage: cell.coverage,
        unmeasured: true,
        tokenSelection: cellSelection(row),
      }];
    }),
  );
  return {
    animation: false,
    grid: { left: 12, right: 16, top: 12, bottom: 56, containLabel: true },
    xAxis: {
      type: "category",
      data: TOKEN_HEATMAP_COLUMNS.map(heatmapColumnLabel),
      axisLabel: { fontSize: 12, interval: 0, hideOverlap: true },
    },
    yAxis: {
      type: "category",
      data: view.heatmap.map((row) => row.label),
      inverse: true,
      axisLabel: { fontSize: 12, width: 180, overflow: "truncate" },
    },
    visualMap: [
      {
        min: 0,
        max: measuredMax,
        seriesIndex: 0,
        calculable: false,
        orient: "horizontal",
        left: "center",
        bottom: 0,
        dimension: 2,
        inRange: { color: [MEASURED_ZERO_COLOR, MEASURED_HIGH_COLOR] },
        formatter: (value: unknown) =>
          typeof value === "number" && Number.isFinite(value) ? formatTokenTick(value) : "",
      },
      {
        min: 0,
        max: 1,
        seriesIndex: 1,
        show: false,
        dimension: 2,
        inRange: { color: [UNKNOWN_CELL_COLOR, UNKNOWN_CELL_COLOR] },
      },
    ],
    series: [
      {
        type: "heatmap",
        name: "measured",
        data: measuredData,
        label: {
          show: true,
          fontSize: 11,
          formatter: (value: unknown) => {
            const datum = chartDatum(value);
            if (!datum || datum.exact === null) return "—";
            return formatTokenTick(assertChartNumber(datum.exact));
          },
        },
      },
      {
        type: "heatmap",
        name: "unmeasured",
        data: unmeasuredData,
        itemStyle: { color: UNKNOWN_CELL_COLOR, borderColor: "#6e675c", borderWidth: 1 },
        emphasis: { itemStyle: { color: UNKNOWN_CELL_COLOR, borderColor: "#6e675c", borderWidth: 1 } },
        label: {
          show: true,
          fontSize: 11,
          color: "#3d3830",
          formatter: () => "—",
        },
      },
    ],
    tooltip: {
      renderMode: "html",
      formatter: (value: unknown) => chartTooltip(value),
    },
  };
}

export function tokenHeatmapHeight(rowCount: number): number {
  return Math.max(360, 96 + rowCount * 36);
}

export function runConsumptionOption(view: TokenUsageView): EChartsCoreOption {
  const categories = view.buckets.map(
    (bucket) => `${configuredModelLabel(bucket.provider, bucket.configuredModel)} · ${roleLabel(bucket.role)}`,
  );
  const series = TOKEN_FLOW_NAMES.map((name) => ({
    type: "bar",
    stack: "direct-consumption",
    name: flowLabel(name),
    itemStyle: { color: TOKEN_FLOW_COLORS[name] },
    data: view.buckets.map((bucket) => {
      const leaf = bucket.flows.find((item) => item.name === name);
      const exact = leaf && leaf.knownTotal !== null && leaf.knownTotal > 0 ? leaf.knownTotal : null;
      return {
        value: exact === null ? null : assertChartNumber(exact),
        displayLabel: `${configuredModelLabel(bucket.provider, bucket.configuredModel)} · ${roleLabel(bucket.role)} · ${flowLabel(name)}`,
        exact,
        coverage: leaf?.coverage ?? "unknown",
        tokenSelection: selection("leaf", bucket),
      };
    }),
  }));
  return {
    animation: false,
    grid: { left: 12, right: 16, top: 12, bottom: 48, containLabel: true },
    legend: { bottom: 0, type: "scroll", textStyle: { fontSize: 12 } },
    xAxis: {
      type: "value",
      min: 0,
      axisLabel: {
        fontSize: 12,
        formatter: (value: number) => formatTokenTick(value),
      },
    },
    yAxis: {
      type: "category",
      data: categories,
      inverse: true,
      axisLabel: { fontSize: 12, width: 220, overflow: "truncate" },
    },
    series,
    tooltip: {
      renderMode: "html",
      formatter: (value: unknown) => chartTooltip(value),
    },
  };
}

export function runConsumptionHeight(barCount: number): number {
  return Math.max(240, 84 + barCount * 36);
}

function chartRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : {};
}

function chartDatum(value: unknown): (ChartDatum & { displayLabel: string }) | null {
  const item = chartRecord(value);
  const data = chartRecord(item.data ?? item);
  if (typeof data.displayLabel !== "string") return null;
  const exact = data.exact;
  return {
    displayLabel: data.displayLabel,
    exact: typeof exact === "number" || exact === null ? exact as number | null : null,
    coverage: typeof data.coverage === "string" ? data.coverage as TokenCoverage : "unknown",
    tokenSelection: data.tokenSelection as TokenSelectionTarget,
  };
}

function chartLabel(value: unknown): string {
  return chartDatum(value)?.displayLabel ?? "";
}

function chartTooltip(value: unknown): string {
  const datum = chartDatum(value);
  if (!datum) return "";
  return tooltipHtml(datum.displayLabel, datum.exact, datum.coverage);
}

export function tokenSelectionFromChartEvent(value: unknown): TokenSelectionTarget | null {
  const item = chartRecord(value);
  const data = chartRecord(item.data ?? item);
  const selectionValue = chartRecord(data.tokenSelection);
  const kind = selectionValue.kind;
  const provider = selectionValue.provider;
  const role = selectionValue.role;
  if (kind !== "model" && kind !== "role" && kind !== "leaf") return null;
  if (provider !== "cursor" && provider !== "codex") return null;
  if (role !== null && role !== "implementer" && role !== "reviewer") return null;
  const configuredModel = selectionValue.configuredModel;
  if (configuredModel !== null && typeof configuredModel !== "string") return null;
  if (kind !== "model" && role === null) return null;
  return {
    kind,
    provider,
    configuredModel: configuredModel ?? null,
    role: kind === "model" ? null : role,
  };
}

export function facetsFromSelection(
  target: TokenSelectionTarget,
  current: TokenFacets = EMPTY_TOKEN_FACETS,
): TokenFacets {
  const model = { provider: target.provider, configuredModel: target.configuredModel };
  const key = modelFacetKey(model);
  const selected = current.models.some((item) => modelFacetKey(item) === key);
  if (target.kind === "model") {
    return {
      models: selected ? current.models.filter((item) => modelFacetKey(item) !== key) : [...current.models, model],
      role: current.role,
    };
  }
  return { models: selected ? current.models : [...current.models, model], role: target.role };
}

export function isUnsupportedTokenUsage(error: unknown): boolean {
  if (!(error instanceof DashboardDataSourceError)) return false;
  if (error.code === "UNSUPPORTED") return true;
  return error.code === "INVALID_ARGUMENT"
    && error.message === "Unsupported dashboard read operation.";
}

export function nativeCategoryLabel(name: string): string {
  const labels: Record<string, string> = {
    input_tokens: "Input tokens",
    output_tokens: "Output tokens",
    cache_read_tokens: "Cache read tokens",
    cache_write_tokens: "Cache write tokens",
    total_tokens: "Total tokens",
    reasoning_tokens: "Reasoning tokens",
    cached_input_tokens: "Cached input tokens",
    cache_write_input_tokens: "Cache write input tokens",
    reasoning_output_tokens: "Reasoning output tokens",
  };
  return labels[name] ?? name;
}
