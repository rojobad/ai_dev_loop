import type { EChartsCoreOption } from "echarts/core";

import type {
  CapacityLimit,
  DashboardProjectSummary,
  DashboardWorkloadItem,
} from "./types";

const STATE_SERIES = [
  ["Completed", "completed", "#16816d"],
  ["Residual risk", "residualRisk", "#b8780b"],
  ["Running", "running", "#2f76d2"],
  ["Waiting", "waiting", "#8054ae"],
  ["Blocked", "blocked", "#c64e64"],
  ["Aborted", "aborted", "#727984"],
  ["Other", "other", "#52606d"],
] as const;

export function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character] ?? character,
  );
}

function eventRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

export function projectStateOption(projects: DashboardProjectSummary[]): EChartsCoreOption {
  return {
    animation: false,
    grid: { left: 12, right: 18, top: 18, bottom: 68, containLabel: true },
    legend: { bottom: 0, type: "scroll" },
    tooltip: {
      trigger: "axis",
      renderMode: "html",
      formatter: (value: unknown) => {
        const items = Array.isArray(value) ? value.map(eventRecord) : [eventRecord(value)];
        const first = items[0] ?? {};
        const project = typeof first.name === "string" ? first.name : "Project";
        const rows = items
          .map((item) => {
            const label = typeof item.seriesName === "string" ? item.seriesName : "State";
            const data = eventRecord(item.data);
            const count = typeof item.value === "number"
              ? item.value
              : typeof data.value === "number"
                ? data.value
                : 0;
            const otherStates = Array.isArray(data.otherStates)
              ? data.otherStates
                  .map((entry) => {
                    const state = eventRecord(entry);
                    return typeof state.state === "string" && typeof state.count === "number"
                      ? `${escapeHtml(state.state)} (${state.count})`
                      : null;
                  })
                  .filter((entry): entry is string => entry !== null)
              : [];
            return `${escapeHtml(label)}: ${count}${otherStates.length ? `<br>${otherStates.join("<br>")}` : ""}`;
          })
          .join("<br>");
        return `<strong>${escapeHtml(project)}</strong><br>${rows}`;
      },
    },
    xAxis: { type: "value", minInterval: 1, name: "Started runs" },
    yAxis: {
      type: "category",
      data: projects.map((project) => project.projectName),
      axisLabel: { width: 150, overflow: "truncate" },
    },
    series: STATE_SERIES.map(([label, key, color]) => ({
      name: label,
      type: "bar",
      stack: "state",
      emphasis: { focus: "series" },
      itemStyle: { color },
      data: projects.map((project) =>
        key === "other"
          ? { value: project.stateCounts[key], otherStates: project.otherStates }
          : project.stateCounts[key],
      ),
    })),
  };
}

interface WorkloadNode {
  name: string;
  value: number;
  projectName: string;
  sequenceId?: string | null;
  sequenceName?: string | null;
  children?: WorkloadNode[];
}

export function workloadOption(items: DashboardWorkloadItem[]): EChartsCoreOption {
  const projects = new Map<string, WorkloadNode[]>();
  for (const item of items) {
    const group = item.sequenceShortName ?? "Standalone";
    const children = projects.get(item.projectName) ?? [];
    children.push({
      name: group,
      value: item.runCount,
      projectName: item.projectName,
      sequenceId: item.sequenceId,
      sequenceName: item.sequenceName,
    });
    projects.set(item.projectName, children);
  }
  const data: WorkloadNode[] = [...projects.entries()].map(([projectName, children]) => ({
    name: projectName,
    value: children.reduce((sum, child) => sum + child.value, 0),
    projectName,
    children,
  }));
  return {
    animation: false,
    tooltip: {
      renderMode: "html",
      formatter: (value: unknown) => {
        const item = eventRecord(value);
        const dataItem = eventRecord(item.data);
        const name = typeof item.name === "string" ? item.name : "Workload";
        const project =
          typeof dataItem.projectName === "string" ? dataItem.projectName : name;
        const count = typeof item.value === "number" ? item.value : 0;
        const sequenceName =
          typeof dataItem.sequenceName === "string" ? dataItem.sequenceName : null;
        const details = sequenceName
          ? `<br>${escapeHtml(sequenceName)}`
          : "";
        return `<strong>${escapeHtml(project)}</strong><br>${escapeHtml(name)}: ${count} runs${details}`;
      },
    },
    series: [
      {
        type: "treemap",
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        label: { show: true, formatter: "{b}" },
        upperLabel: { show: true, height: 24 },
        data,
      },
    ],
  };
}

export function capacityOption(limit: CapacityLimit): EChartsCoreOption {
  return {
    animation: false,
    series: [
      {
        type: "gauge",
        startAngle: 205,
        endAngle: -25,
        min: 0,
        max: 100,
        radius: "92%",
        progress: { show: true, width: 12, itemStyle: { color: "#16816d" } },
        axisLine: { lineStyle: { width: 12 } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        pointer: { show: false },
        anchor: { show: false },
        title: { show: false },
        detail: {
          valueAnimation: false,
          formatter: "{value}%",
          fontSize: 24,
          offsetCenter: [0, "6%"],
        },
        data: [{ value: limit.remainingPercent }],
      },
    ],
  };
}

export function projectFromChartEvent(value: unknown): string | null {
  const event = eventRecord(value);
  const data = eventRecord(event.data);
  if (typeof data.projectName === "string") return data.projectName;
  return typeof event.name === "string" ? event.name : null;
}
