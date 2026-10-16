import type {
  CodexCapacityData,
  DashboardCoverage,
  DashboardDataSource,
  DashboardFilters,
  DashboardProjectsData,
  DashboardSummaryData,
  SourceObservation,
} from "./types";

export const completeCoverage: DashboardCoverage = {
  dataState: "complete",
  unstartedRuns: 0,
  missingLaunchTimes: 0,
  unknownExecutionKinds: 0,
  missingDurationAttempts: 0,
  warnings: [],
};

export function summaryData(
  runsStarted = 7,
  projectName: string | null = null,
): DashboardSummaryData {
  if (projectName === "project-b") {
    return {
      observedAt: "2026-01-03T00:00:00Z",
      filters: {
        fromUtc: "2026-01-01T00:00:00Z",
        toUtc: "2026-01-03T00:00:00Z",
        timeZone: "UTC",
        projectName,
      },
      coverage: completeCoverage,
      totals: { runsStarted: 3, completed: 1, residualRisk: 1, waitingBlocked: 1 },
      projects: [
        {
          projectName: "project-b",
          total: 3,
          stateCounts: {
            completed: 1,
            residualRisk: 1,
            running: 0,
            waiting: 0,
            blocked: 1,
            aborted: 0,
            other: 0,
          },
        },
      ],
      workload: [
        {
          projectName: "project-b",
          sequenceId: "seq-b",
          sequenceName: "Delivery",
          runCount: 3,
        },
      ],
    };
  }
  return {
    observedAt: "2026-01-03T00:00:00Z",
    filters: {
      fromUtc: "2026-01-01T00:00:00Z",
      toUtc: "2026-01-03T00:00:00Z",
      timeZone: "UTC",
      projectName,
    },
    coverage: completeCoverage,
    totals: {
      runsStarted,
      completed: Math.min(2, runsStarted),
      residualRisk: Math.min(1, Math.max(0, runsStarted - 2)),
      waitingBlocked: runsStarted >= 3 ? 1 : 0,
    },
    projects:
      runsStarted >= 7
        ? [
            {
              projectName: "project-a<script>alert(1)</script>",
              total: 4,
              stateCounts: {
                completed: 2,
                residualRisk: 1,
                running: 0,
                waiting: 0,
                blocked: 0,
                aborted: 0,
                other: 1,
              },
            },
            {
              projectName: "project-b",
              total: 3,
              stateCounts: {
                completed: 0,
                residualRisk: 0,
                running: 1,
                waiting: 1,
                blocked: 0,
                aborted: 1,
                other: 0,
              },
            },
          ]
        : [
            {
              projectName: "project-a",
              total: runsStarted,
              stateCounts: {
                completed: Math.min(2, runsStarted),
                residualRisk: Math.min(1, Math.max(0, runsStarted - 2)),
                running: 0,
                waiting: runsStarted >= 4 ? 1 : 0,
                blocked: 0,
                aborted: 0,
                other: Math.max(0, runsStarted - 4),
              },
            },
          ],
    workload:
      runsStarted >= 7
        ? [
            {
              projectName: "project-a<script>alert(1)</script>",
              sequenceId: "seq-a",
              sequenceName: "Build",
              runCount: 2,
            },
            {
              projectName: "project-a<script>alert(1)</script>",
              sequenceId: null,
              sequenceName: null,
              runCount: 2,
            },
            {
              projectName: "project-b",
              sequenceId: "seq-b",
              sequenceName: "Delivery",
              runCount: 3,
            },
          ]
        : [
            {
              projectName: "project-a",
              sequenceId: null,
              sequenceName: null,
              runCount: runsStarted,
            },
          ],
  };
}

export const capacityData: CodexCapacityData = {
  status: "available",
  reason: null,
  limits: [
    {
      id: "primary",
      window: "5-hour window",
      usedPercent: 36,
      remainingPercent: 64,
      windowDurationMinutes: 300,
      resetsAt: "2026-01-03T02:00:00Z",
    },
    {
      id: "weekly",
      window: "Weekly window",
      usedPercent: 73,
      remainingPercent: 27,
      windowDurationMinutes: null,
      resetsAt: null,
    },
  ],
};

export function observation<T>(data: T): SourceObservation<T> {
  return {
    data,
    source: "local",
    observedAt: "2026-01-03T00:00:00Z",
    receivedAt: "2026-01-03T00:00:01Z",
  };
}

export class ImmediateDataSource implements DashboardDataSource {
  readonly summaryCalls: DashboardFilters[] = [];
  capacityCalls = 0;

  async projects(
    _page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardProjectsData>> {
    return observation({
      observedAt: "2026-01-03T00:00:00Z",
      items: [
        { projectName: "project-a<script>alert(1)</script>", runCount: 4 },
        { projectName: "project-b", runCount: 3 },
      ],
      page: { offset: 0, limit: 100, nextOffset: null, hasMore: false },
      totalCount: 2,
    });
  }

  async summary(
    filters: DashboardFilters,
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardSummaryData>> {
    this.summaryCalls.push(filters);
    return observation(summaryData(filters.projectName === "project-b" ? 3 : 7, filters.projectName ?? null));
  }

  async capacity(_signal: AbortSignal): Promise<SourceObservation<CodexCapacityData>> {
    this.capacityCalls += 1;
    return observation(capacityData);
  }
}
