import type {
  CapacityLimit,
  CodexCapacityData,
  DashboardCoverage,
  DashboardFilterEcho,
  DashboardPage,
  DashboardProjectItem,
  DashboardProjectSummary,
  DashboardProjectsData,
  DashboardStateCounts,
  DashboardSummaryData,
  DashboardSummaryTotals,
  DashboardWorkloadItem,
} from "./types";

type JsonRecord = Record<string, unknown>;

function record(value: unknown, context: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new TypeError(`${context} must be an object.`);
  }
  return value as JsonRecord;
}

function array(value: unknown, context: string): unknown[] {
  if (!Array.isArray(value)) {
    throw new TypeError(`${context} must be an array.`);
  }
  return value;
}

function text(value: unknown, context: string, allowEmpty = false): string {
  if (typeof value !== "string" || (!allowEmpty && value.length === 0)) {
    throw new TypeError(`${context} must be a string.`);
  }
  return value;
}

function nullableText(value: unknown, context: string): string | null {
  return value === null ? null : text(value, context);
}

function utcInstant(value: unknown, context: string): string {
  const result = text(value, context);
  if (!result.endsWith("Z") || !Number.isFinite(Date.parse(result))) {
    throw new TypeError(`${context} must be a UTC RFC3339 timestamp.`);
  }
  return result;
}

function nullableUtcInstant(value: unknown, context: string): string | null {
  return value === null ? null : utcInstant(value, context);
}

function finiteNumber(value: unknown, context: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < minimum) {
    throw new TypeError(`${context} must be a finite number.`);
  }
  return value;
}

function integer(value: unknown, context: string, minimum = 0): number {
  const result = finiteNumber(value, context, minimum);
  if (!Number.isInteger(result)) {
    throw new TypeError(`${context} must be an integer.`);
  }
  return result;
}

function boolean(value: unknown, context: string): boolean {
  if (typeof value !== "boolean") {
    throw new TypeError(`${context} must be a boolean.`);
  }
  return value;
}

function nullableInteger(value: unknown, context: string): number | null {
  return value === null ? null : integer(value, context, 1);
}

function filters(value: unknown): DashboardFilterEcho {
  const item = record(value, "filters");
  return {
    fromUtc: utcInstant(item.fromUtc, "filters.fromUtc"),
    toUtc: utcInstant(item.toUtc, "filters.toUtc"),
    timeZone: text(item.timeZone, "filters.timeZone"),
    projectName: nullableText(item.projectName, "filters.projectName"),
  };
}

function coverage(value: unknown): DashboardCoverage {
  const item = record(value, "coverage");
  const dataState = text(item.dataState, "coverage.dataState");
  if (!new Set(["complete", "partial", "empty"]).has(dataState)) {
    throw new TypeError("coverage.dataState is unsupported.");
  }
  return {
    dataState: dataState as DashboardCoverage["dataState"],
    unstartedRuns: integer(item.unstartedRuns, "coverage.unstartedRuns"),
    missingLaunchTimes: integer(item.missingLaunchTimes, "coverage.missingLaunchTimes"),
    unknownExecutionKinds: integer(
      item.unknownExecutionKinds,
      "coverage.unknownExecutionKinds",
    ),
    missingDurationAttempts: integer(
      item.missingDurationAttempts,
      "coverage.missingDurationAttempts",
    ),
    warnings: array(item.warnings, "coverage.warnings").map((warning, index) =>
      text(warning, `coverage.warnings[${index}]`),
    ),
  };
}

function page(value: unknown): DashboardPage {
  const item = record(value, "page");
  const nextOffset = item.nextOffset === null ? null : integer(item.nextOffset, "page.nextOffset");
  const hasMore = boolean(item.hasMore, "page.hasMore");
  if (hasMore !== (nextOffset !== null)) {
    throw new TypeError("page.nextOffset presence must match page.hasMore.");
  }
  return {
    offset: integer(item.offset, "page.offset"),
    limit: integer(item.limit, "page.limit", 1),
    nextOffset,
    hasMore,
  };
}

function projectItem(value: unknown, index: number): DashboardProjectItem {
  const item = record(value, `items[${index}]`);
  return {
    projectName: text(item.projectName, `items[${index}].projectName`),
    runCount: integer(item.runCount, `items[${index}].runCount`),
  };
}

export function parseProjectsData(value: unknown): DashboardProjectsData {
  const data = record(value, "dashboard.projects data");
  return {
    observedAt: utcInstant(data.observedAt, "observedAt"),
    items: array(data.items, "items").map(projectItem),
    page: page(data.page),
    totalCount: integer(data.totalCount, "totalCount"),
  };
}

function stateCounts(value: unknown, context: string): DashboardStateCounts {
  const item = record(value, context);
  return {
    completed: integer(item.completed, `${context}.completed`),
    residualRisk: integer(item.residualRisk, `${context}.residualRisk`),
    running: integer(item.running, `${context}.running`),
    waiting: integer(item.waiting, `${context}.waiting`),
    blocked: integer(item.blocked, `${context}.blocked`),
    aborted: integer(item.aborted, `${context}.aborted`),
    other: integer(item.other, `${context}.other`),
  };
}

function projectSummary(value: unknown, index: number): DashboardProjectSummary {
  const item = record(value, `projects[${index}]`);
  return {
    projectName: text(item.projectName, `projects[${index}].projectName`),
    total: integer(item.total, `projects[${index}].total`),
    stateCounts: stateCounts(item.stateCounts, `projects[${index}].stateCounts`),
  };
}

function totals(value: unknown): DashboardSummaryTotals {
  const item = record(value, "totals");
  return {
    runsStarted: integer(item.runsStarted, "totals.runsStarted"),
    completed: integer(item.completed, "totals.completed"),
    residualRisk: integer(item.residualRisk, "totals.residualRisk"),
    waitingBlocked: integer(item.waitingBlocked, "totals.waitingBlocked"),
  };
}

function workload(value: unknown, index: number): DashboardWorkloadItem {
  const item = record(value, `workload[${index}]`);
  return {
    projectName: text(item.projectName, `workload[${index}].projectName`),
    sequenceId: nullableText(item.sequenceId, `workload[${index}].sequenceId`),
    sequenceName: nullableText(item.sequenceName, `workload[${index}].sequenceName`),
    runCount: integer(item.runCount, `workload[${index}].runCount`),
  };
}

export function parseSummaryData(value: unknown): DashboardSummaryData {
  const data = record(value, "dashboard.summary data");
  const result: DashboardSummaryData = {
    observedAt: utcInstant(data.observedAt, "observedAt"),
    filters: filters(data.filters),
    coverage: coverage(data.coverage),
    totals: totals(data.totals),
    projects: array(data.projects, "projects").map(projectSummary),
    workload: array(data.workload, "workload").map(workload),
  };
  const projectTotal = result.projects.reduce((sum, item) => sum + item.total, 0);
  const workloadTotal = result.workload.reduce((sum, item) => sum + item.runCount, 0);
  if (projectTotal !== result.totals.runsStarted || workloadTotal !== result.totals.runsStarted) {
    throw new TypeError("Summary totals do not match complete project/workload counts.");
  }
  for (const project of result.projects) {
    const counts = project.stateCounts;
    const total =
      counts.completed +
      counts.residualRisk +
      counts.running +
      counts.waiting +
      counts.blocked +
      counts.aborted +
      counts.other;
    if (total !== project.total) {
      throw new TypeError("Project state counts do not match the project total.");
    }
  }
  const completed = result.projects.reduce(
    (sum, project) => sum + project.stateCounts.completed,
    0,
  );
  const residualRisk = result.projects.reduce(
    (sum, project) => sum + project.stateCounts.residualRisk,
    0,
  );
  const waitingBlocked = result.projects.reduce(
    (sum, project) => sum + project.stateCounts.waiting + project.stateCounts.blocked,
    0,
  );
  if (
    completed !== result.totals.completed ||
    residualRisk !== result.totals.residualRisk ||
    waitingBlocked !== result.totals.waitingBlocked
  ) {
    throw new TypeError("Summary state groups do not match the public KPI totals.");
  }
  return result;
}

function capacityLimit(value: unknown, index: number): CapacityLimit {
  const item = record(value, `limits[${index}]`);
  const usedPercent = finiteNumber(item.usedPercent, `limits[${index}].usedPercent`);
  const remainingPercent = finiteNumber(
    item.remainingPercent,
    `limits[${index}].remainingPercent`,
  );
  if (usedPercent > 100 || remainingPercent > 100) {
    throw new TypeError(`limits[${index}] percentages must not exceed 100.`);
  }
  return {
    id: text(item.id, `limits[${index}].id`),
    window: text(item.window, `limits[${index}].window`),
    usedPercent,
    remainingPercent,
    windowDurationMinutes: nullableInteger(
      item.windowDurationMinutes,
      `limits[${index}].windowDurationMinutes`,
    ),
    resetsAt: nullableUtcInstant(item.resetsAt, `limits[${index}].resetsAt`),
  };
}

export function parseCapacityData(value: unknown): CodexCapacityData {
  const data = record(value, "machine.codex-capacity data");
  return {
    status: text(data.status, "status"),
    reason: nullableText(data.reason, "reason"),
    limits: array(data.limits, "limits").map(capacityLimit),
  };
}
