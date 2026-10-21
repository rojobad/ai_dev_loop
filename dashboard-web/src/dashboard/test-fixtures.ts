import type {
  AttemptTimelineData,
  CodexCapacityData,
  DashboardActivityData,
  DashboardActivityMetric,
  DashboardCoverage,
  DashboardDataSource,
  DashboardFilters,
  DashboardProjectsData,
  DashboardRunItem,
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
  TokenCategory,
  TokenCoverage,
  TokenFlow,
  TokenFlowName,
  TokenProviderName,
  TokenRoleName,
  TokenStageName,
  TokenUsageData,
  TokenUsageRow,
} from "./types";
import { TOKEN_FLOW_NAMES } from "./types";

export const completeCoverage: DashboardCoverage = {
  dataState: "complete",
  unstartedRuns: 0,
  missingLaunchTimes: 0,
  unknownExecutionKinds: 0,
  missingDurationAttempts: 0,
  warnings: [],
};

export function activityData(
  metric: DashboardActivityMetric = "submissions",
  filters: DashboardFilters = {
    fromUtc: "2026-01-01T00:00:00Z",
    toUtc: "2026-01-03T00:00:00Z",
    timeZone: "UTC",
  },
): DashboardActivityData {
  const process = metric === "process-hours";
  const days = process
    ? [
        { date: "2026-01-01", value: 5400 },
        { date: "2026-01-02", value: 1800 },
      ]
    : [
        { date: "2026-01-01", value: metric === "review-rounds" ? 2 : 3 },
        { date: "2026-01-02", value: 1 },
      ];
  return {
    observedAt: "2026-01-03T00:00:00Z",
    filters: {
      fromUtc: filters.fromUtc,
      toUtc: filters.toUtc,
      timeZone: filters.timeZone,
      projectName: filters.projectName ?? null,
    },
    coverage: completeCoverage,
    metric,
    unit: process ? "process-seconds" : "count",
    days,
    total: days.reduce((sum, item) => sum + item.value, 0),
  };
}

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
          otherStates: [],
        },
      ],
      workload: [
        {
          projectName: "project-b",
          sequenceId: "seq-b",
          sequenceName: "Delivery",
          sequenceShortName: "Delivery",
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
              otherStates: [{ state: '<future-state onmouseover="bad">', count: 1 }],
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
              otherStates: [],
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
              otherStates:
                runsStarted > 4
                  ? [{ state: "future_state", count: runsStarted - 4 }]
                  : [],
            },
          ],
    workload:
      runsStarted >= 7
        ? [
            {
              projectName: "project-a<script>alert(1)</script>",
              sequenceId: "seq-a",
              sequenceName: "Build",
              sequenceShortName: "Build",
              runCount: 2,
            },
            {
              projectName: "project-a<script>alert(1)</script>",
              sequenceId: null,
              sequenceName: null,
              sequenceShortName: null,
              runCount: 2,
            },
            {
              projectName: "project-b",
              sequenceId: "seq-b",
              sequenceName: "Delivery",
              sequenceShortName: "Delivery",
              runCount: 3,
            },
          ]
        : [
            {
              projectName: "project-a",
              sequenceId: null,
              sequenceName: null,
              sequenceShortName: null,
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

export const standaloneRun: DashboardRunItem = {
  runId: "standalone-run",
  shortName: "Standalone run",
  projectName: "project-a",
  implementerProvider: "cursor",
  state: "completed",
  submittedAt: "2026-01-01T00:00:00Z",
  firstExecutionLaunchAt: "2026-01-01T01:00:00Z",
  sequenceBinding: null,
};

export function inspectData(runId: string): RunInspectData {
  return {
    implementerProvider: "cursor",
    codexPendingRole: null,
    codexPendingTurn: null,
    codexCapacityReason: null,
    runId,
    shortName: "Standalone run",
    sequenceShortName: null,
    projectName: "project-a",
    repositoryRoot: "/fixture/project-a",
    state: "completed",
    submittedAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-01T04:00:00Z",
    reviewBudget: { completed: 1, max: 3, submittedMax: null },
    sequenceBinding: null,
    residualRisk: false,
    blockReason: null,
    cursorWaitUntil: null,
    implementerRetryWaitUntil: null,
    implementerTimeoutAutomaticRetries: 0,
    implementerTimeoutRetryLimit: 3,
    safeNextAction: { kind: "none", runId, sequenceId: null, waitUntil: null },
    attemptCount: 1,
  };
}

export const oneAttempt: AttemptTimelineData = {
  runId: standaloneRun.runId,
  items: [
    {
      attemptId: "attempt-cursor",
      component: "cursor",
      effectKind: "cursor.run_turn",
      iteration: 1,
      phaseAttempt: 1,
      status: "completed",
      createdAt: "2026-01-01T01:00:00Z",
      launchRequestedAt: "2026-01-01T01:00:00Z",
      completedAt: "2026-01-01T02:00:00Z",
      observedDurationSeconds: 3600,
    },
  ],
  page: { offset: 0, limit: 25, nextOffset: null, hasMore: false },
};

function tokenCategory(knownTotal: number | null, coverage?: TokenCoverage): TokenCategory {
  const resolved = coverage ?? (knownTotal === null ? "unknown" : "complete");
  return {
    knownTotal,
    knownObservations: knownTotal === null ? 0 : 1,
    observationCount: 1,
    coverage: resolved,
  };
}

function tokenFlows(values: Partial<Record<TokenFlowName, number | null>>): TokenFlow[] {
  return TOKEN_FLOW_NAMES.map((name) => {
    const knownTotal = Object.prototype.hasOwnProperty.call(values, name) ? values[name] ?? null : null;
    return {
      name,
      knownTotal,
      knownObservations: knownTotal === null ? 0 : 1,
      observationCount: 1,
      coverage: knownTotal === null ? "unknown" : "complete",
    };
  });
}

function tokenRow(input: {
  provider: TokenProviderName;
  configuredModel: string | null;
  role: TokenRoleName;
  stage: TokenStageName;
  knownTotalTokens: number | null;
  coverage: TokenCoverage;
  inferenceAttempts?: number;
  knownAttempts?: number;
  partialAttempts?: number;
  unknownAttempts?: number;
  flows: Partial<Record<TokenFlowName, number | null>>;
  categories: Record<string, number | null>;
  nativeCategories?: Record<string, number | null>;
}): TokenUsageRow {
  const inferenceAttempts = input.inferenceAttempts ?? 1;
  const knownAttempts = input.knownAttempts ?? (input.coverage === "complete" ? inferenceAttempts : 0);
  const partialAttempts = input.partialAttempts ?? (input.coverage === "partial" ? 1 : 0);
  const unknownAttempts = input.unknownAttempts ?? (input.coverage === "unknown" ? inferenceAttempts : 0);
  return {
    provider: input.provider,
    configuredModel: input.configuredModel,
    modelProvenance: "configured",
    role: input.role,
    stage: input.stage,
    knownTotalTokens: input.knownTotalTokens,
    inferenceAttempts,
    knownAttempts,
    partialAttempts,
    unknownAttempts,
    coverage: input.coverage,
    categories: Object.fromEntries(
      Object.entries(input.categories).map(([name, knownTotal]) => [
        name,
        tokenCategory(
          knownTotal,
          input.coverage === "partial" && knownTotal !== null ? "partial" : undefined,
        ),
      ]),
    ),
    nativeCategories: Object.fromEntries(
      Object.entries(input.nativeCategories ?? {}).map(([name, knownTotal]) => [
        name,
        tokenCategory(knownTotal),
      ]),
    ),
    flows: tokenFlows(input.flows),
  };
}

/** Accepted 29.1 literals: Cursor 140, Codex 115, mixed leaves 180/40/5/24/6. */
export function cursorCodexTokenUsage(
  filters: DashboardFilters = {
    fromUtc: "2026-10-01T00:00:00Z",
    toUtc: "2026-10-02T00:00:00Z",
    timeZone: "America/La_Paz",
  },
  extras: TokenUsageRow[] = [],
): TokenUsageData {
  const cursor = tokenRow({
    provider: "cursor",
    configuredModel: "shared",
    role: "implementer",
    stage: "initial",
    knownTotalTokens: 140,
    coverage: "complete",
    flows: {
      input_other: 100,
      cache_read: 20,
      cache_write: 5,
      output_other: 12,
      reasoning_output: 3,
    },
    categories: {
      input_total: 125,
      input_uncached: 100,
      cache_read: 20,
      cache_write: 5,
      output_total: 15,
      reasoning_output: 3,
      total_tokens: 140,
    },
    nativeCategories: { cache_write_tokens: 5, reasoning_tokens: 3, total_tokens: 140 },
  });
  const codex = tokenRow({
    provider: "codex",
    configuredModel: "shared",
    role: "implementer",
    stage: "initial",
    knownTotalTokens: 115,
    coverage: "complete",
    flows: {
      input_other: 80,
      cache_read: 20,
      output_other: 12,
      reasoning_output: 3,
    },
    categories: {
      input_total: 100,
      input_uncached: 80,
      cache_read: 20,
      cache_write: 7,
      output_total: 15,
      reasoning_output: 3,
      total_tokens: 115,
    },
    nativeCategories: { cache_write_input_tokens: 7, total_tokens: 115 },
  });
  const rows = [codex, cursor, ...extras];
  const knownRows = rows.filter((row) => row.knownTotalTokens !== null);
  return {
    observedAt: "2026-10-19T12:05:00Z",
    filters: {
      fromUtc: filters.fromUtc,
      toUtc: filters.toUtc,
      timeZone: filters.timeZone,
      projectName: filters.projectName ?? null,
    },
    scope: "cohort",
    modelProvenance: "configured",
    normalizationVersion: "provider-usage-v1",
    selectedRun: null,
    selectedRunCount: rows.length,
    cohortRunCount: rows.length,
    cohortCoverage: completeCoverage,
    source: {
      knownTotalTokens: knownRows.reduce((sum, row) => sum + (row.knownTotalTokens ?? 0), 0),
      inferenceAttempts: rows.reduce((sum, row) => sum + row.inferenceAttempts, 0),
      knownAttempts: rows.reduce((sum, row) => sum + row.knownAttempts, 0),
      partialAttempts: rows.reduce((sum, row) => sum + row.partialAttempts, 0),
      unknownAttempts: rows.reduce((sum, row) => sum + row.unknownAttempts, 0),
      coverage: rows.some((row) => row.coverage !== "complete") ? "partial" : "complete",
      diagnostics: [],
      categories: {},
      flows: TOKEN_FLOW_NAMES.map((name) => {
        const amounts = rows.flatMap((row) => {
          const leaf = row.flows.find((item) => item.name === name);
          return leaf?.knownTotal === null || leaf === undefined ? [] : [leaf.knownTotal];
        });
        return {
          name,
          knownTotal: amounts.length === 0 ? null : amounts.reduce((sum, value) => sum + value, 0),
          knownObservations: amounts.length,
          observationCount: rows.length,
          coverage: amounts.length === rows.length ? "complete" as const : amounts.length === 0 ? "unknown" as const : "partial" as const,
        };
      }),
    },
    rows,
  };
}

export function mixedTokenUsage(filters?: DashboardFilters): TokenUsageData {
  return cursorCodexTokenUsage(filters, [
    tokenRow({
      provider: "cursor",
      configuredModel: null,
      role: "implementer",
      stage: "initial",
      knownTotalTokens: null,
      coverage: "unknown",
      knownAttempts: 0,
      unknownAttempts: 1,
      flows: {},
      categories: {
        input_total: null,
        input_uncached: null,
        cache_read: null,
        cache_write: null,
        output_total: null,
        reasoning_output: null,
      },
    }),
    tokenRow({
      provider: "cursor",
      configuredModel: "recorded-zero",
      role: "implementer",
      stage: "initial",
      knownTotalTokens: 0,
      coverage: "complete",
      flows: {
        input_other: 0,
        cache_read: 0,
        cache_write: 0,
        output_other: 0,
        reasoning_output: 0,
      },
      categories: {
        input_total: 0,
        input_uncached: 0,
        cache_read: 0,
        cache_write: 0,
        output_total: 0,
        reasoning_output: 0,
      },
    }),
    tokenRow({
      provider: "cursor",
      configuredModel: "partial-model",
      role: "reviewer",
      stage: "correction",
      knownTotalTokens: 10,
      coverage: "partial",
      inferenceAttempts: 2,
      knownAttempts: 1,
      partialAttempts: 1,
      unknownAttempts: 0,
      flows: { input_other: 10 },
      categories: { input_total: 10, input_uncached: 10, output_total: null, reasoning_output: null },
    }),
    tokenRow({
      provider: "codex",
      configuredModel: 'review<img alt="x">',
      role: "reviewer",
      stage: "review",
      knownTotalTokens: 10,
      coverage: "complete",
      flows: { input_unsplit: 8, output_unsplit: 2 },
      categories: { input_total: 8, output_total: 2, cache_read: null, reasoning_output: null },
    }),
  ]);
}

export class ImmediateDataSource implements DashboardDataSource {
  readonly summaryCalls: DashboardFilters[] = [];
  readonly tokenCalls: Array<{ filters: DashboardFilters; runId: string | null }> = [];
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

  async runs(
    filters: DashboardFilters,
    page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardRunsData>> {
    const items = filters.projectName === "project-b" ? [] : [standaloneRun];
    return observation({
      observedAt: "2026-01-03T00:00:00Z",
      filters: {
        fromUtc: filters.fromUtc,
        toUtc: filters.toUtc,
        timeZone: filters.timeZone,
        projectName: filters.projectName ?? null,
      },
      coverage: completeCoverage,
      items: page.offset === 0 ? items : [],
      page: { offset: page.offset, limit: page.limit, nextOffset: null, hasMore: false },
      totalCount: items.length,
    });
  }

  async activity(
    filters: DashboardFilters,
    metric: DashboardActivityMetric,
    _signal: AbortSignal,
  ): Promise<SourceObservation<DashboardActivityData>> {
    return observation(activityData(metric, filters));
  }

  async runInspect(
    runId: string,
    _signal: AbortSignal,
  ): Promise<SourceObservation<RunInspectData>> {
    return observation(inspectData(runId));
  }

  async runTimeline(
    runId: string,
    page: { offset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<AttemptTimelineData>> {
    return observation({
      ...oneAttempt,
      runId,
      items: page.offset === 0 ? oneAttempt.items : [],
      page: { offset: page.offset, limit: page.limit, nextOffset: null, hasMore: false },
    });
  }

  async initialPrompt(
    _runId: string,
    _read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<FrozenPromptChunk>> {
    throw new Error("Content is not configured in this fixture.");
  }

  async review(
    _runId: string,
    _attemptId: string,
    _signal: AbortSignal,
  ): Promise<SourceObservation<ReviewDetailData>> {
    throw new Error("Review content is not configured in this fixture.");
  }

  async reviewContent(
    _runId: string,
    _attemptId: string,
    _kind: ReviewContentKind,
    _read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<ReviewContentChunk>> {
    throw new Error("Review content is not configured in this fixture.");
  }

  async runOutput(
    _runId: string,
    _attemptId: string,
    _stream: ProcessStream,
    _read: { byteOffset: number; limit: number },
    _signal: AbortSignal,
  ): Promise<SourceObservation<ProcessOutputChunk>> {
    throw new Error("Process output is not configured in this fixture.");
  }

  async capacity(_signal: AbortSignal): Promise<SourceObservation<CodexCapacityData>> {
    this.capacityCalls += 1;
    return observation(capacityData);
  }

  async tokenUsage(
    filters: DashboardFilters,
    runId: string | null,
    _signal: AbortSignal,
  ): Promise<SourceObservation<TokenUsageData>> {
    this.tokenCalls.push({ filters, runId });
    if (runId === null) return observation(cursorCodexTokenUsage(filters));
    const direct = cursorCodexTokenUsage(filters);
    const row = direct.rows.find((item) => item.provider === "cursor") ?? direct.rows[0];
    return observation({
      ...direct,
      scope: "direct",
      selectedRun: { runId, shortName: "Selected run", projectName: filters.projectName ?? "project-a" },
      selectedRunCount: 1,
      cohortRunCount: Math.max(1, direct.cohortRunCount),
      source: {
        ...direct.source,
        knownTotalTokens: row.knownTotalTokens,
        inferenceAttempts: row.inferenceAttempts,
        knownAttempts: row.knownAttempts,
        partialAttempts: row.partialAttempts,
        unknownAttempts: row.unknownAttempts,
        coverage: row.coverage,
        flows: row.flows,
        categories: row.categories,
      },
      rows: [row],
    });
  }
}
