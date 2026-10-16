import { describe, expect, it } from "vitest";

import {
  projectFromChartEvent,
  projectStateOption,
  workloadOption,
} from "./charts";
import type { DashboardProjectSummary, DashboardWorkloadItem } from "./types";

interface TooltipOption {
  formatter: (value: unknown) => string;
}

interface StateSeries {
  data: number[];
  name: string;
  stack: string;
  type: string;
}

interface WorkloadNode {
  children?: WorkloadNode[];
  name: string;
  projectName: string;
  value: number;
}

function tooltipFormatter(option: unknown): TooltipOption["formatter"] {
  return (option as { tooltip: TooltipOption }).tooltip.formatter;
}

const projects: DashboardProjectSummary[] = [
  {
    projectName: "alpha",
    total: 7,
    stateCounts: {
      completed: 1,
      residualRisk: 2,
      running: 3,
      waiting: 0,
      blocked: 1,
      aborted: 0,
      other: 0,
    },
  },
  {
    projectName: "beta",
    total: 8,
    stateCounts: {
      completed: 2,
      residualRisk: 1,
      running: 0,
      waiting: 1,
      blocked: 0,
      aborted: 3,
      other: 1,
    },
  },
];

const workload: DashboardWorkloadItem[] = [
  { projectName: "alpha", sequenceId: "seq-a", sequenceName: "Sequence A", runCount: 3 },
  { projectName: "alpha", sequenceId: null, sequenceName: null, runCount: 2 },
  { projectName: "beta", sequenceId: "seq-b", sequenceName: "Sequence B", runCount: 5 },
];

describe("chart projections", () => {
  it("projects every independently expected project/state series", () => {
    const option = projectStateOption(projects);
    expect((option.yAxis as { data: string[] }).data).toEqual(["alpha", "beta"]);
    expect(option.series as StateSeries[]).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ name: "Completed", type: "bar", stack: "state", data: [1, 2] }),
        expect.objectContaining({
          name: "Residual risk",
          type: "bar",
          stack: "state",
          data: [2, 1],
        }),
        expect.objectContaining({ name: "Running", type: "bar", stack: "state", data: [3, 0] }),
        expect.objectContaining({ name: "Waiting", type: "bar", stack: "state", data: [0, 1] }),
        expect.objectContaining({ name: "Blocked", type: "bar", stack: "state", data: [1, 0] }),
        expect.objectContaining({ name: "Aborted", type: "bar", stack: "state", data: [0, 3] }),
        expect.objectContaining({ name: "Other", type: "bar", stack: "state", data: [0, 1] }),
      ]),
    );
    expect(option.series).toHaveLength(7);
  });

  it("projects exact sequence and Standalone workload leaves and counts", () => {
    const option = workloadOption(workload);
    const series = option.series as Array<{ data: WorkloadNode[] }>;
    expect(series[0].data).toEqual([
      {
        name: "alpha",
        projectName: "alpha",
        value: 5,
        children: [
          { name: "Sequence A", projectName: "alpha", value: 3 },
          { name: "Standalone", projectName: "alpha", value: 2 },
        ],
      },
      {
        name: "beta",
        projectName: "beta",
        value: 5,
        children: [{ name: "Sequence B", projectName: "beta", value: 5 }],
      },
    ]);
  });

  it("escapes malicious labels at both production HTML tooltip formatters", () => {
    const maliciousProject = '<img src=x onerror="boom"> & project';
    const stateFormatter = tooltipFormatter(
      projectStateOption([{ ...projects[0], projectName: maliciousProject }]),
    );
    expect(
      stateFormatter([
        { name: maliciousProject, seriesName: "Completed", value: 1 },
        { name: maliciousProject, seriesName: "Other", value: 2 },
      ]),
    ).toBe(
      "<strong>&lt;img src=x onerror=&quot;boom&quot;&gt; &amp; project</strong><br>Completed: 1<br>Other: 2",
    );

    const maliciousSequence = '<script>alert("leaf")</script> & sequence';
    const workloadOptionValue = workloadOption([
      {
        projectName: maliciousProject,
        sequenceId: "seq-malicious",
        sequenceName: maliciousSequence,
        runCount: 4,
      },
    ]);
    const workloadSeries = workloadOptionValue.series as Array<{ data: WorkloadNode[] }>;
    const leaf = workloadSeries[0].data[0].children?.[0];
    expect(leaf).toBeDefined();
    expect(
      tooltipFormatter(workloadOptionValue)({
        name: leaf?.name,
        value: leaf?.value,
        data: leaf,
      }),
    ).toBe(
      "<strong>&lt;img src=x onerror=&quot;boom&quot;&gt; &amp; project</strong><br>&lt;script&gt;alert(&quot;leaf&quot;)&lt;/script&gt; &amp; sequence: 4 runs",
    );
  });

  it("maps production chart events to an authoritative project selection", () => {
    expect(projectFromChartEvent({ name: "display", data: { projectName: "project-from-data" } })).toBe(
      "project-from-data",
    );
    expect(projectFromChartEvent({ name: "project-from-axis" })).toBe("project-from-axis");
    expect(projectFromChartEvent({ data: { projectName: 42 } })).toBeNull();
    expect(projectFromChartEvent(null)).toBeNull();
  });
});
