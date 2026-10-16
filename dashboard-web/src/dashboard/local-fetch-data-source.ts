import {
  parseAttemptTimelineData,
  parseCapacityData,
  parseProjectsData,
  parseRunInspectData,
  parseRunsData,
  parseSummaryData,
} from "./parsers";
import {
  DASHBOARD_API_MAJOR,
  DashboardDataSourceError,
  type AttemptTimelineData,
  type CodexCapacityData,
  type DashboardDataSource,
  type DashboardFilters,
  type DashboardProjectsData,
  type DashboardRunsData,
  type DashboardSummaryData,
  type RunInspectData,
  type SourceObservation,
} from "./types";

type Parser<T> = (value: unknown) => T;

function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new DashboardDataSourceError("INVALID_RESPONSE", "Dashboard response was invalid.");
  }
  return value as Record<string, unknown>;
}

export class LocalFetchDashboardDataSource implements DashboardDataSource {
  private async read<T>(
    operation: string,
    parameters: Record<string, unknown>,
    parser: Parser<T>,
    signal: AbortSignal,
  ): Promise<SourceObservation<T>> {
    let response: Response;
    try {
      response = await fetch("/api/dashboard/read", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ operation, parameters }),
        cache: "no-store",
        credentials: "same-origin",
        signal,
      });
    } catch (error) {
      if (signal.aborted) {
        throw error;
      }
      throw new DashboardDataSourceError(
        "NETWORK_ERROR",
        "The local dashboard service could not be reached.",
      );
    }

    const receivedAt = new Date().toISOString();
    let payload: Record<string, unknown>;
    try {
      payload = object(await response.json());
    } catch {
      throw new DashboardDataSourceError(
        "INVALID_RESPONSE",
        "The local dashboard service returned invalid JSON.",
      );
    }
    const version = object(payload.apiVersion);
    if (version.major !== DASHBOARD_API_MAJOR) {
      throw new DashboardDataSourceError(
        "UPDATE_REQUIRED",
        "The local dashboard API major version is not supported.",
      );
    }
    if (!response.ok || payload.ok !== true) {
      const error = object(payload.error);
      const code = response.status === 503
        ? "SERVICE_UNAVAILABLE"
        : typeof error.code === "string"
          ? error.code
          : "INTERNAL_ERROR";
      const message =
        typeof error.message === "string"
          ? error.message
          : "The local dashboard read failed.";
      throw new DashboardDataSourceError(code, message);
    }
    const observedAt = payload.observedAt;
    if (
      typeof observedAt !== "string" ||
      !observedAt.endsWith("Z") ||
      !Number.isFinite(Date.parse(observedAt))
    ) {
      throw new DashboardDataSourceError(
        "INVALID_RESPONSE",
        "The local dashboard observation time was missing.",
      );
    }
    try {
      return {
        data: parser(payload.data),
        source: "local",
        observedAt,
        receivedAt,
      };
    } catch (error) {
      if (error instanceof DashboardDataSourceError) {
        throw error;
      }
      throw new DashboardDataSourceError(
        "INVALID_RESPONSE",
        "The local dashboard response did not match its typed contract.",
      );
    }
  }

  projects(
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardProjectsData>> {
    return this.read("dashboard.projects", page, parseProjectsData, signal);
  }

  summary(
    filters: DashboardFilters,
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardSummaryData>> {
    return this.read("dashboard.summary", { ...filters }, parseSummaryData, signal);
  }

  runs(
    filters: DashboardFilters,
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>> {
    return this.read("dashboard.runs", { ...filters, ...page }, parseRunsData, signal);
  }

  runInspect(
    runId: string,
    signal: AbortSignal,
  ): Promise<SourceObservation<RunInspectData>> {
    return this.read("run.inspect", { runId }, parseRunInspectData, signal);
  }

  runTimeline(
    runId: string,
    page: { offset: number; limit: number },
    signal: AbortSignal,
  ): Promise<SourceObservation<AttemptTimelineData>> {
    return this.read(
      "run.timeline",
      { runId, ...page },
      parseAttemptTimelineData,
      signal,
    );
  }

  capacity(signal: AbortSignal): Promise<SourceObservation<CodexCapacityData>> {
    return this.read("machine.codex-capacity", {}, parseCapacityData, signal);
  }
}
