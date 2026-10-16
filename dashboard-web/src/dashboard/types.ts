export const DASHBOARD_API_MAJOR = 1;
export const DEFAULT_TIME_ZONE = "America/La_Paz";

export type DataState = "complete" | "partial" | "empty";

export interface DashboardFilters {
  fromUtc: string;
  toUtc: string;
  timeZone: string;
  projectName?: string;
}

export interface DashboardFilterEcho {
  fromUtc: string;
  toUtc: string;
  timeZone: string;
  projectName: string | null;
}

export interface DashboardCoverage {
  dataState: DataState;
  unstartedRuns: number;
  missingLaunchTimes: number;
  unknownExecutionKinds: number;
  missingDurationAttempts: number;
  warnings: string[];
}

export interface DashboardPage {
  offset: number;
  limit: number;
  nextOffset: number | null;
  hasMore: boolean;
}

export interface DashboardProjectItem {
  projectName: string;
  runCount: number;
}

export interface DashboardProjectsData {
  observedAt: string;
  items: DashboardProjectItem[];
  page: DashboardPage;
  totalCount: number;
}

export interface DashboardStateCounts {
  completed: number;
  residualRisk: number;
  running: number;
  waiting: number;
  blocked: number;
  aborted: number;
  other: number;
}

export interface DashboardSummaryTotals {
  runsStarted: number;
  completed: number;
  residualRisk: number;
  waitingBlocked: number;
}

export interface DashboardProjectSummary {
  projectName: string;
  total: number;
  stateCounts: DashboardStateCounts;
}

export interface DashboardWorkloadItem {
  projectName: string;
  sequenceId: string | null;
  sequenceName: string | null;
  runCount: number;
}

export interface DashboardSummaryData {
  observedAt: string;
  filters: DashboardFilterEcho;
  coverage: DashboardCoverage;
  totals: DashboardSummaryTotals;
  projects: DashboardProjectSummary[];
  workload: DashboardWorkloadItem[];
}

export interface CapacityLimit {
  id: string;
  window: string;
  usedPercent: number;
  remainingPercent: number;
  windowDurationMinutes: number | null;
  resetsAt: string | null;
}

export interface CodexCapacityData {
  status: string;
  reason: string | null;
  limits: CapacityLimit[];
}

export interface SourceObservation<T> {
  data: T;
  source: "local";
  observedAt: string;
  receivedAt: string;
}

export interface DashboardDataSource {
  projects(
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardProjectsData>>;
  summary(
    filters: DashboardFilters,
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardSummaryData>>;
  capacity(signal: AbortSignal): Promise<SourceObservation<CodexCapacityData>>;
}

export class DashboardDataSourceError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "DashboardDataSourceError";
    this.code = code;
  }
}
