import type {
  AttemptTimelineData,
  AttemptTimelineItem,
  ArtifactContentChunk,
  CapacityLimit,
  CodexCapacityData,
  DashboardCoverage,
  DashboardFilterEcho,
  DashboardPage,
  DashboardProjectItem,
  DashboardProjectSummary,
  DashboardProjectsData,
  DashboardRunItem,
  DashboardRunsData,
  DashboardSequenceBinding,
  DashboardStateCounts,
  DashboardSummaryData,
  DashboardSummaryTotals,
  DashboardWorkloadItem,
  IntegrationRunSequenceBinding,
  IntegrationSafeNextAction,
  FrozenPromptChunk,
  ProcessOutputAvailability,
  ProcessOutputChunk,
  ProcessStream,
  ReviewContentAvailability,
  ReviewContentChunk,
  ReviewContentKind,
  ReviewDetailData,
  RunInspectData,
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

function optionalNullableText(value: unknown, context: string): string | null {
  return value === undefined || value === null ? null : text(value, context);
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

function optionalNullableInteger(
  value: unknown,
  context: string,
  minimum = 0,
): number | null {
  return value === undefined || value === null ? null : integer(value, context, minimum);
}

function optionalNullableBoolean(value: unknown, context: string): boolean | null {
  return value === undefined || value === null ? null : boolean(value, context);
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
  const offset = integer(item.offset, "page.offset");
  const limit = integer(item.limit, "page.limit", 1);
  if (nextOffset !== null && nextOffset !== offset + limit) {
    throw new TypeError("page.nextOffset must advance by page.limit.");
  }
  return {
    offset,
    limit,
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
  const otherStates = array(item.otherStates, `projects[${index}].otherStates`).map(
    (value, otherIndex) => {
      const other = record(value, `projects[${index}].otherStates[${otherIndex}]`);
      return {
        state: text(other.state, `projects[${index}].otherStates[${otherIndex}].state`),
        count: integer(other.count, `projects[${index}].otherStates[${otherIndex}].count`, 1),
      };
    },
  );
  const rawStates = otherStates.map((other) => other.state);
  if (new Set(rawStates).size !== rawStates.length) {
    throw new TypeError("Project otherStates must contain unique raw state labels.");
  }
  return {
    projectName: text(item.projectName, `projects[${index}].projectName`),
    total: integer(item.total, `projects[${index}].total`),
    stateCounts: stateCounts(item.stateCounts, `projects[${index}].stateCounts`),
    otherStates,
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
    const otherTotal = project.otherStates.reduce((sum, item) => sum + item.count, 0);
    if (otherTotal !== counts.other) {
      throw new TypeError("Project otherStates do not match stateCounts.other.");
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

function dashboardSequenceBinding(value: unknown, context: string): DashboardSequenceBinding {
  const item = record(value, context);
  return {
    sequenceId: text(item.sequenceId, `${context}.sequenceId`),
    sequenceName: text(item.sequenceName, `${context}.sequenceName`),
    ordinal: integer(item.ordinal, `${context}.ordinal`, 1),
    phaseName: text(item.phaseName, `${context}.phaseName`),
    generation: integer(item.generation, `${context}.generation`, 1),
    attemptKind: text(item.attemptKind, `${context}.attemptKind`),
    sourceRunId: nullableText(item.sourceRunId, `${context}.sourceRunId`),
  };
}

function dashboardRun(value: unknown, index: number): DashboardRunItem {
  const context = `items[${index}]`;
  const item = record(value, context);
  const provider = text(item.implementerProvider, `${context}.implementerProvider`);
  if (provider !== "cursor" && provider !== "codex") {
    throw new TypeError(`${context}.implementerProvider is unsupported.`);
  }
  return {
    runId: text(item.runId, `${context}.runId`),
    projectName: text(item.projectName, `${context}.projectName`),
    implementerProvider: provider,
    state: text(item.state, `${context}.state`),
    submittedAt: utcInstant(item.submittedAt, `${context}.submittedAt`),
    firstExecutionLaunchAt: utcInstant(
      item.firstExecutionLaunchAt,
      `${context}.firstExecutionLaunchAt`,
    ),
    sequenceBinding:
      item.sequenceBinding === null
        ? null
        : dashboardSequenceBinding(item.sequenceBinding, `${context}.sequenceBinding`),
  };
}

export function parseRunsData(value: unknown): DashboardRunsData {
  const data = record(value, "dashboard.runs data");
  return {
    observedAt: utcInstant(data.observedAt, "observedAt"),
    filters: filters(data.filters),
    coverage: coverage(data.coverage),
    items: array(data.items, "items").map(dashboardRun),
    page: page(data.page),
    totalCount: integer(data.totalCount, "totalCount"),
  };
}

function runSequenceBinding(value: unknown): IntegrationRunSequenceBinding | null {
  if (value === undefined || value === null) return null;
  const item = record(value, "sequenceBinding");
  return {
    sequenceId: text(item.sequenceId, "sequenceBinding.sequenceId"),
    ordinal: integer(item.ordinal, "sequenceBinding.ordinal", 1),
    phaseCount: integer(item.phaseCount, "sequenceBinding.phaseCount", 1),
  };
}

function safeNextAction(value: unknown): IntegrationSafeNextAction {
  const item = record(value, "safeNextAction");
  return {
    kind: text(item.kind, "safeNextAction.kind"),
    runId: optionalNullableText(item.runId, "safeNextAction.runId"),
    sequenceId: optionalNullableText(item.sequenceId, "safeNextAction.sequenceId"),
    waitUntil: optionalNullableText(item.waitUntil, "safeNextAction.waitUntil"),
  };
}

export function parseRunInspectData(value: unknown): RunInspectData {
  const data = record(value, "run.inspect data");
  const provider = data.implementerProvider === undefined
    ? "cursor"
    : text(data.implementerProvider, "implementerProvider");
  if (provider !== "cursor" && provider !== "codex") {
    throw new TypeError("implementerProvider is unsupported.");
  }
  const reviewBudget = record(data.reviewBudget, "reviewBudget");
  return {
    implementerProvider: provider,
    codexPendingRole: optionalNullableText(data.codexPendingRole, "codexPendingRole"),
    codexPendingTurn: optionalNullableText(data.codexPendingTurn, "codexPendingTurn"),
    codexCapacityReason: optionalNullableText(data.codexCapacityReason, "codexCapacityReason"),
    runId: text(data.runId, "runId"),
    projectName: text(data.projectName, "projectName"),
    repositoryRoot: text(data.repositoryRoot, "repositoryRoot"),
    state: text(data.state, "state"),
    submittedAt: utcInstant(data.submittedAt, "submittedAt"),
    updatedAt: utcInstant(data.updatedAt, "updatedAt"),
    reviewBudget: {
      completed: integer(reviewBudget.completed, "reviewBudget.completed"),
      max: integer(reviewBudget.max, "reviewBudget.max"),
      submittedMax: optionalNullableInteger(reviewBudget.submittedMax, "reviewBudget.submittedMax"),
    },
    sequenceBinding: runSequenceBinding(data.sequenceBinding),
    residualRisk: optionalNullableBoolean(data.residualRisk, "residualRisk"),
    blockReason: optionalNullableText(data.blockReason, "blockReason"),
    cursorWaitUntil: optionalNullableText(data.cursorWaitUntil, "cursorWaitUntil"),
    implementerRetryWaitUntil: optionalNullableText(
      data.implementerRetryWaitUntil,
      "implementerRetryWaitUntil",
    ),
    implementerTimeoutAutomaticRetries: optionalNullableInteger(
      data.implementerTimeoutAutomaticRetries,
      "implementerTimeoutAutomaticRetries",
    ),
    implementerTimeoutRetryLimit: optionalNullableInteger(
      data.implementerTimeoutRetryLimit,
      "implementerTimeoutRetryLimit",
    ),
    safeNextAction: safeNextAction(data.safeNextAction),
    attemptCount: integer(data.attemptCount, "attemptCount"),
  };
}

function timelineItem(value: unknown, index: number): AttemptTimelineItem {
  const context = `items[${index}]`;
  const item = record(value, context);
  return {
    attemptId: text(item.attemptId, `${context}.attemptId`),
    component: text(item.component, `${context}.component`),
    effectKind: nullableText(item.effectKind, `${context}.effectKind`),
    iteration: integer(item.iteration, `${context}.iteration`, 1),
    phaseAttempt: integer(item.phaseAttempt, `${context}.phaseAttempt`, 1),
    status: text(item.status, `${context}.status`),
    createdAt: utcInstant(item.createdAt, `${context}.createdAt`),
    launchRequestedAt:
      item.launchRequestedAt === null
        ? null
        : utcInstant(item.launchRequestedAt, `${context}.launchRequestedAt`),
    completedAt:
      item.completedAt === null
        ? null
        : utcInstant(item.completedAt, `${context}.completedAt`),
    observedDurationSeconds:
      item.observedDurationSeconds === null
        ? null
        : finiteNumber(item.observedDurationSeconds, `${context}.observedDurationSeconds`),
  };
}

export function parseAttemptTimelineData(value: unknown): AttemptTimelineData {
  const data = record(value, "run.timeline data");
  return {
    runId: text(data.runId, "runId"),
    items: array(data.items, "items").map(timelineItem),
    page: page(data.page),
  };
}

function contentAvailability(value: unknown, context: string) {
  const item = record(value, context);
  const available = boolean(item.available, `${context}.available`);
  const reason = nullableText(item.reason, `${context}.reason`);
  if (available === (reason !== null)) {
    throw new TypeError(`${context} availability and reason are inconsistent.`);
  }
  return { available, reason };
}

function reviewContentAvailability(value: unknown): ReviewContentAvailability {
  const item = record(value, "content");
  return {
    prompt: contentAvailability(item.prompt, "content.prompt"),
    response: contentAvailability(item.response, "content.response"),
    reviewMarkdown: contentAvailability(item.reviewMarkdown, "content.reviewMarkdown"),
    cursorFixPrompt: contentAvailability(item.cursorFixPrompt, "content.cursorFixPrompt"),
  };
}

function processStreamAvailability(value: unknown, context: string) {
  const item = record(value, context);
  const stream = text(item.stream, `${context}.stream`);
  if (stream !== "stdout" && stream !== "stderr") {
    throw new TypeError(`${context}.stream is unsupported.`);
  }
  return {
    stream: stream as ProcessStream,
    ...contentAvailability(item, context),
  };
}

function processOutputAvailability(value: unknown): ProcessOutputAvailability {
  const item = record(value, "processOutput");
  return {
    stdout: processStreamAvailability(item.stdout, "processOutput.stdout"),
    stderr: processStreamAvailability(item.stderr, "processOutput.stderr"),
  };
}

export function parseReviewDetailData(value: unknown): ReviewDetailData {
  const data = record(value, "run.review data");
  return {
    runId: text(data.runId, "runId"),
    attemptId: text(data.attemptId, "attemptId"),
    iteration: integer(data.iteration, "iteration", 1),
    phaseAttempt: integer(data.phaseAttempt, "phaseAttempt", 1),
    status: text(data.status, "status"),
    reviewMode: text(data.reviewMode, "reviewMode"),
    createdAt: utcInstant(data.createdAt, "createdAt"),
    launchRequestedAt: nullableUtcInstant(data.launchRequestedAt, "launchRequestedAt"),
    completedAt: nullableUtcInstant(data.completedAt, "completedAt"),
    resultState: text(data.resultState, "resultState"),
    content: reviewContentAvailability(data.content),
    processOutput: processOutputAvailability(data.processOutput),
  };
}

function artifactChunk(value: unknown, context: string): ArtifactContentChunk {
  const data = record(value, context);
  const available = boolean(data.available, `${context}.available`);
  const reason = nullableText(data.reason, `${context}.reason`);
  const encoding = text(data.encoding, `${context}.encoding`);
  if (encoding !== "base64") throw new TypeError(`${context}.encoding is unsupported.`);
  const byteOffset = integer(data.byteOffset, `${context}.byteOffset`);
  const returnedBytes = integer(data.returnedBytes, `${context}.returnedBytes`);
  const nextOffset = data.nextOffset === null
    ? null
    : integer(data.nextOffset, `${context}.nextOffset`);
  const availableBytes = data.availableBytes === null
    ? null
    : integer(data.availableBytes, `${context}.availableBytes`);
  const hasMore = boolean(data.hasMore, `${context}.hasMore`);
  const contentBase64 = text(data.contentBase64, `${context}.contentBase64`, true);
  const sha256 = nullableText(data.sha256, `${context}.sha256`);
  if (available) {
    if (
      reason !== null || availableBytes === null ||
      byteOffset + returnedBytes > availableBytes
    ) {
      throw new TypeError(`${context} available metadata is inconsistent.`);
    }
    if (hasMore && nextOffset === null) throw new TypeError(`${context} continuation is missing.`);
  } else if (
    reason === null || returnedBytes !== 0 || contentBase64.length > 0 ||
    nextOffset !== null || availableBytes !== null || hasMore
  ) {
    throw new TypeError(`${context} unavailable metadata is inconsistent.`);
  }
  return {
    available,
    reason,
    encoding: "base64",
    mediaType: nullableText(data.mediaType, `${context}.mediaType`),
    byteOffset,
    returnedBytes,
    nextOffset,
    availableBytes,
    hasMore,
    contentBase64,
    sha256,
  };
}

export function parseFrozenPromptChunk(value: unknown): FrozenPromptChunk {
  const data = record(value, "run.initial-prompt data");
  const chunk = artifactChunk(data, "run.initial-prompt data");
  if (chunk.hasMore !== (chunk.nextOffset !== null)) {
    throw new TypeError("run.initial-prompt continuation metadata is inconsistent.");
  }
  return {
    ...chunk,
    runId: text(data.runId, "runId"),
    artifactKind: text(data.artifactKind, "artifactKind"),
  };
}

const REVIEW_CONTENT_KINDS = new Set<ReviewContentKind>([
  "prompt",
  "response",
  "review-markdown",
  "cursor-fix-prompt",
]);

export function parseReviewContentChunk(value: unknown): ReviewContentChunk {
  const data = record(value, "run.review-content data");
  const kind = text(data.contentKind, "contentKind") as ReviewContentKind;
  if (!REVIEW_CONTENT_KINDS.has(kind)) throw new TypeError("contentKind is unsupported.");
  const chunk = artifactChunk(data, "run.review-content data");
  if (chunk.hasMore !== (chunk.nextOffset !== null)) {
    throw new TypeError("run.review-content continuation metadata is inconsistent.");
  }
  return {
    ...chunk,
    runId: text(data.runId, "runId"),
    attemptId: text(data.attemptId, "attemptId"),
    contentKind: kind,
  };
}

export function parseProcessOutputChunk(value: unknown): ProcessOutputChunk {
  const data = record(value, "run.output data");
  const stream = text(data.stream, "stream");
  if (stream !== "stdout" && stream !== "stderr") {
    throw new TypeError("stream is unsupported.");
  }
  const chunk = artifactChunk(data, "run.output data");
  const complete = boolean(data.complete, "complete");
  if (chunk.available && !chunk.hasMore && complete && chunk.nextOffset !== null) {
    throw new TypeError("Completed output must not expose a continuation offset at EOF.");
  }
  if (chunk.available && !chunk.hasMore && !complete && chunk.nextOffset === null) {
    throw new TypeError("Active output must expose its observed EOF offset.");
  }
  return {
    ...chunk,
    runId: text(data.runId, "runId"),
    attemptId: text(data.attemptId, "attemptId"),
    component: text(data.component, "component"),
    stream: stream as ProcessStream,
    format: text(data.format, "format"),
    processState: text(data.processState, "processState"),
    complete,
    truncatedAtSource: optionalNullableBoolean(data.truncatedAtSource, "truncatedAtSource"),
    storedBytes: optionalNullableInteger(data.storedBytes, "storedBytes"),
  };
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
