import { RefreshCoordinator, canonicalReadKey } from "./refreshCoordinator";
import type {
  AttemptTimelineData,
  CodexCapacityData,
  DashboardActivityData,
  DashboardActivityMetric,
  DashboardDataSource,
  DashboardFilters,
  DashboardProjectsData,
  DashboardRunsData,
  DashboardSummaryData,
  FrozenPromptChunk,
  ProcessOutputChunk,
  ProcessStream,
  ReviewContentChunk,
  ReviewContentKind,
  ReviewDetailData,
  RunInspectData,
  SourceObservation,
  TokenUsageData,
} from "./types";

/** Queues deliberate component reads behind the same two-slot coordinator. */
export class CoordinatedDashboardDataSource implements DashboardDataSource {
  constructor(
    private readonly delegate: DashboardDataSource,
    private readonly coordinator: RefreshCoordinator,
  ) {}

  projects(
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardProjectsData>> {
    return this.coordinator.request(
      canonicalReadKey("dashboard.projects", page),
      (owned) => this.delegate.projects(page, owned),
      signal,
    );
  }

  summary(
    filters: DashboardFilters,
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardSummaryData>> {
    return this.coordinator.request(
      canonicalReadKey("dashboard.summary.once", filterParameters(filters)),
      (owned) => this.delegate.summary(filters, owned),
      signal,
    );
  }

  runs(
    filters: DashboardFilters,
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>> {
    return this.coordinator.request(
      canonicalReadKey("dashboard.runs.once", { ...filterParameters(filters), ...page }),
      (owned) => this.delegate.runs(filters, page, owned),
      signal,
    );
  }

  activity(
    filters: DashboardFilters,
    metric: DashboardActivityMetric,
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardActivityData>> {
    return this.coordinator.request(
      canonicalReadKey("dashboard.activity.once", { ...filterParameters(filters), metric }),
      (owned) => this.delegate.activity(filters, metric, owned),
      signal,
    );
  }

  runInspect(runId: string, signal: AbortSignal): Promise<SourceObservation<RunInspectData>> {
    return this.coordinator.request(
      canonicalReadKey("run.inspect.once", { runId }),
      (owned) => this.delegate.runInspect(runId, owned),
      signal,
    );
  }

  runTimeline(
    runId: string,
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<AttemptTimelineData>> {
    return this.coordinator.request(
      canonicalReadKey("run.timeline.once", { runId, ...page }),
      (owned) => this.delegate.runTimeline(runId, page, owned),
      signal,
    );
  }

  initialPrompt(
    runId: string,
    read: { byteOffset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<FrozenPromptChunk>> {
    return this.coordinator.request(
      canonicalReadKey("run.initial-prompt", { runId, ...read }),
      (owned) => this.delegate.initialPrompt(runId, read, owned),
      signal,
    );
  }

  review(
    runId: string,
    attemptId: string,
    signal: AbortSignal,
  ): Promise<SourceObservation<ReviewDetailData>> {
    return this.coordinator.request(
      canonicalReadKey("run.review", { runId, attemptId }),
      (owned) => this.delegate.review(runId, attemptId, owned),
      signal,
    );
  }

  reviewContent(
    runId: string,
    attemptId: string,
    kind: ReviewContentKind,
    read: { byteOffset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<ReviewContentChunk>> {
    return this.coordinator.request(
      canonicalReadKey("run.review-content", { runId, attemptId, kind, ...read }),
      (owned) => this.delegate.reviewContent(runId, attemptId, kind, read, owned),
      signal,
    );
  }

  runOutput(
    runId: string,
    attemptId: string,
    stream: ProcessStream,
    read: { byteOffset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<ProcessOutputChunk>> {
    return this.coordinator.request(
      canonicalReadKey("run.output", { runId, attemptId, stream, ...read }),
      (owned) => this.delegate.runOutput(runId, attemptId, stream, read, owned),
      signal,
    );
  }

  capacity(signal: AbortSignal): Promise<SourceObservation<CodexCapacityData>> {
    return this.coordinator.request(
      "machine.codex-capacity.once",
      (owned) => this.delegate.capacity(owned),
      signal,
    );
  }

  tokenUsage(
    filters: DashboardFilters,
    runId: string | null,
    signal: AbortSignal,
  ): Promise<SourceObservation<TokenUsageData>> {
    return this.coordinator.request(
      canonicalReadKey("dashboard.token-usage.once", {
        ...filterParameters(filters),
        runId,
      }),
      (owned) => this.delegate.tokenUsage(filters, runId, owned),
      signal,
    );
  }
}

function filterParameters(
  filters: DashboardFilters,
): Record<string, string | null | undefined> {
  return {
    fromUtc: filters.fromUtc,
    toUtc: filters.toUtc,
    timeZone: filters.timeZone,
    projectName: filters.projectName ?? null,
  };
}
