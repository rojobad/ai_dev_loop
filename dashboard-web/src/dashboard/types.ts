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
  otherStates: DashboardOtherState[];
}

export interface DashboardOtherState {
  state: string;
  count: number;
}

export interface DashboardWorkloadItem {
  projectName: string;
  sequenceId: string | null;
  sequenceName: string | null;
  sequenceShortName: string | null;
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

export interface DashboardSequenceBinding {
  sequenceId: string;
  sequenceName: string;
  sequenceShortName: string;
  ordinal: number;
  phaseName: string;
  generation: number;
  attemptKind: string;
  sourceRunId: string | null;
}

export interface DashboardRunItem {
  runId: string;
  shortName: string;
  projectName: string;
  implementerProvider: "cursor" | "codex";
  state: string;
  submittedAt: string;
  firstExecutionLaunchAt: string;
  sequenceBinding: DashboardSequenceBinding | null;
}

export interface DashboardRunsData {
  observedAt: string;
  filters: DashboardFilterEcho;
  coverage: DashboardCoverage;
  items: DashboardRunItem[];
  page: DashboardPage;
  totalCount: number;
}

export type DashboardActivityMetric = "submissions" | "review-rounds" | "process-hours";

export interface DashboardActivityDay {
  date: string;
  value: number;
}

export interface DashboardActivityData {
  observedAt: string;
  filters: DashboardFilterEcho;
  coverage: DashboardCoverage;
  metric: DashboardActivityMetric;
  unit: "count" | "process-seconds";
  days: DashboardActivityDay[];
  total: number;
}

export interface IntegrationReviewBudget {
  completed: number;
  max: number;
  submittedMax: number | null;
}

export interface IntegrationRunSequenceBinding {
  sequenceId: string;
  ordinal: number;
  phaseCount: number;
}

export interface IntegrationSafeNextAction {
  kind: string;
  runId: string | null;
  sequenceId: string | null;
  waitUntil: string | null;
}

export interface RunInspectData {
  implementerProvider: "cursor" | "codex";
  codexPendingRole: string | null;
  codexPendingTurn: string | null;
  codexCapacityReason: string | null;
  runId: string;
  shortName: string;
  sequenceShortName: string | null;
  projectName: string;
  repositoryRoot: string;
  state: string;
  submittedAt: string;
  updatedAt: string;
  reviewBudget: IntegrationReviewBudget;
  sequenceBinding: IntegrationRunSequenceBinding | null;
  residualRisk: boolean | null;
  blockReason: string | null;
  cursorWaitUntil: string | null;
  implementerRetryWaitUntil: string | null;
  implementerTimeoutAutomaticRetries: number | null;
  implementerTimeoutRetryLimit: number | null;
  safeNextAction: IntegrationSafeNextAction;
  attemptCount: number;
}

export interface AttemptTimelineItem {
  attemptId: string;
  component: string;
  effectKind: string | null;
  iteration: number;
  phaseAttempt: number;
  status: string;
  createdAt: string;
  launchRequestedAt: string | null;
  completedAt: string | null;
  observedDurationSeconds: number | null;
}

export interface AttemptTimelineData {
  runId: string;
  items: AttemptTimelineItem[];
  page: DashboardPage;
}

export type ReviewContentKind =
  | "prompt"
  | "response"
  | "review-markdown"
  | "cursor-fix-prompt";

export type ProcessStream = "stdout" | "stderr";

export interface ContentAvailability {
  available: boolean;
  reason: string | null;
}

export interface ReviewContentAvailability {
  prompt: ContentAvailability;
  response: ContentAvailability;
  reviewMarkdown: ContentAvailability;
  cursorFixPrompt: ContentAvailability;
}

export interface ProcessStreamAvailability extends ContentAvailability {
  stream: ProcessStream;
}

export interface ProcessOutputAvailability {
  stdout: ProcessStreamAvailability;
  stderr: ProcessStreamAvailability;
}

export interface ReviewDetailData {
  runId: string;
  attemptId: string;
  iteration: number;
  phaseAttempt: number;
  status: string;
  reviewMode: string;
  createdAt: string;
  launchRequestedAt: string | null;
  completedAt: string | null;
  resultState: string;
  content: ReviewContentAvailability;
  processOutput: ProcessOutputAvailability;
}

export interface ArtifactContentChunk {
  available: boolean;
  reason: string | null;
  encoding: "base64";
  mediaType: string | null;
  byteOffset: number;
  returnedBytes: number;
  nextOffset: number | null;
  availableBytes: number | null;
  hasMore: boolean;
  contentBase64: string;
  sha256: string | null;
}

export interface FrozenPromptChunk extends ArtifactContentChunk {
  runId: string;
  artifactKind: string;
}

export interface ReviewContentChunk extends ArtifactContentChunk {
  runId: string;
  attemptId: string;
  contentKind: ReviewContentKind;
}

export interface ProcessOutputChunk extends ArtifactContentChunk {
  runId: string;
  attemptId: string;
  component: string;
  stream: ProcessStream;
  format: string;
  processState: string;
  complete: boolean;
  truncatedAtSource: boolean | null;
  storedBytes: number | null;
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
  runs(
    filters: DashboardFilters,
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>>;
  activity(
    filters: DashboardFilters,
    metric: DashboardActivityMetric,
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardActivityData>>;
  runInspect(
    runId: string,
    signal: AbortSignal,
  ): Promise<SourceObservation<RunInspectData>>;
  runTimeline(
    runId: string,
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<AttemptTimelineData>>;
  initialPrompt(
    runId: string,
    read: { byteOffset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<FrozenPromptChunk>>;
  review(
    runId: string,
    attemptId: string,
    signal: AbortSignal,
  ): Promise<SourceObservation<ReviewDetailData>>;
  reviewContent(
    runId: string,
    attemptId: string,
    kind: ReviewContentKind,
    read: { byteOffset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<ReviewContentChunk>>;
  runOutput(
    runId: string,
    attemptId: string,
    stream: ProcessStream,
    read: { byteOffset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<ProcessOutputChunk>>;
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
