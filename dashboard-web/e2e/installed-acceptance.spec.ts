import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

interface FixtureExpectation {
  activeAttemptId: string;
  activityExpectations: Record<
    "submissions" | "review-rounds" | "process-hours",
    {
      days: Array<{ date: string; value: number }>;
      displayDays: string[];
      displayTotal: string;
      total: number;
      unit: "count" | "process-seconds";
    }
  >;
  exactReviewAttemptId: string;
  fromUtc: string;
  historicalReviewAttemptId: string;
  implementerAttemptId: string;
  mapTotal: number;
  projectName: string;
  replacementRunId: string;
  reviewRounds: number;
  runsStarted: number;
  sourceRunId: string;
  submissions: number;
  timelineTotal: number;
  toUtc: string;
}

const baseUrl = process.env.PHASE26_DASHBOARD_BASE_URL;
const fixturePath = process.env.PHASE26_DASHBOARD_FIXTURE;
if (!baseUrl || !fixturePath) {
  throw new Error("Installed dashboard acceptance requires its owned base URL and fixture.");
}
const fixture = JSON.parse(readFileSync(fixturePath, "utf8")) as FixtureExpectation;

function localInput(instant: string): string {
  const value = new Date(instant);
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${pad(value.getUTCDate())}/${pad(value.getUTCMonth() + 1)}/${value.getUTCFullYear()} ${pad(value.getUTCHours())}:${pad(value.getUTCMinutes())}`;
}

async function applyFixtureScope(page: Page): Promise<void> {
  await page.goto(`${baseUrl}/`);
  await page.locator(".filters select").selectOption(fixture.projectName);
  await page.getByLabel("IANA time zone").fill("UTC");
  await page.getByLabel("From").fill(localInput(fixture.fromUtc));
  await page.getByLabel("Until (exclusive)").fill(localInput(fixture.toUtc));
  await page.waitForTimeout(450);
  await expect(page.getByLabel(`Runs started: ${fixture.runsStarted}`)).toBeVisible();
}

async function loadAllRuns(page: Page): Promise<void> {
  await expect(page.getByText(`${fixture.mapTotal}/${fixture.mapTotal} loaded`)).toBeVisible();
}

async function selectRun(page: Page, runId: string): Promise<void> {
  await page.getByLabel("Select loaded run").selectOption(runId);
  await expect(page.getByLabel("Selected run", { exact: true })).toHaveAttribute("data-run-id", runId);
}

async function loadAllAttempts(page: Page): Promise<void> {
  const more = page.getByRole("button", { name: "Load more attempts" });
  if (await more.isVisible()) await more.click();
}

async function assertDisplayedActivity(
  page: Page,
  metric: "submissions" | "review-rounds" | "process-hours",
  label: "Submissions" | "Review rounds" | "Process hours",
): Promise<void> {
  const expected = fixture.activityExpectations[metric];
  await expect(page.locator(".activity-total")).toHaveText(
    `${label} total: ${expected.displayTotal}`,
  );
  const disclosure = page.locator(".activity-days-disclosure");
  if (!(await disclosure.getAttribute("open"))) {
    await disclosure.getByText("Show daily values").click();
  }
  const rows = page.getByLabel(`${label} daily values`).locator("li");
  await expect(rows).toHaveCount(expected.days.length);
  for (const [index, day] of expected.days.entries()) {
    const [year, month, date] = day.date.split("-");
    await expect(rows.nth(index).locator("time")).toHaveText(`${date}/${month}/${year}`);
    await expect(rows.nth(index).locator("span")).toHaveText(expected.displayDays[index]);
  }
}

test("C267-1 installed wheel serves the complete offline provider-aware journey", async ({
  page,
}) => {
  test.setTimeout(60_000);
  const operations: Array<{ operation: string; parameters: Record<string, unknown> }> = [];
  const externalRequests: string[] = [];
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.protocol.startsWith("http") && url.hostname !== "127.0.0.1") {
      externalRequests.push(url.href);
      await route.abort();
      return;
    }
    if (url.pathname === "/api/dashboard/read") {
      const body = JSON.parse(route.request().postData() ?? "{}") as {
        operation?: string;
        parameters?: Record<string, unknown>;
      };
      if (body.operation) {
        operations.push({ operation: body.operation, parameters: body.parameters ?? {} });
      }
    }
    await route.continue();
  });

  await applyFixtureScope(page);
  for (const heading of [
    "Started runs by project and state",
    "Project workload",
    "Current Codex capacity",
    "Sequence and run map",
    "Attempt timeline",
    "Activity calendar",
  ]) {
    await expect(page.getByRole("heading", { name: heading })).toBeVisible();
  }
  await expect(page.getByLabel("Stacked horizontal chart of started runs by project and state")).toBeVisible();
  await expect(page.getByLabel("Treemap of project, sequence, and standalone workload")).toBeVisible();
  await assertDisplayedActivity(page, "submissions", "Submissions");
  await page.getByLabel("Activity metric").selectOption("review-rounds");
  await assertDisplayedActivity(page, "review-rounds", "Review rounds");
  await page.getByLabel("Activity metric").selectOption("process-hours");
  await assertDisplayedActivity(page, "process-hours", "Process hours");
  await expect(page.locator(".coverage").last()).toContainText("missing durations 1");

  await loadAllRuns(page);
  await selectRun(page, fixture.replacementRunId);
  await page.getByRole("button", { name: "Information about Attempt timeline" }).click();
  await expect(page.locator(".lineage-line")).toContainText("implementer_retry");
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByText(`${fixture.timelineTotal > 25 ? 25 : fixture.timelineTotal} loaded / ${fixture.timelineTotal} recorded attempts`)).toBeVisible();
  await loadAllAttempts(page);
  await page.locator(".attempt-details-disclosure > summary").click();

  const sensitiveBefore = operations.filter((call) =>
    ["run.initial-prompt", "run.review", "run.review-content", "run.output"].includes(
      call.operation,
    ),
  ).length;
  await page.getByRole("button", { name: new RegExp(`Attempt ID: ${fixture.implementerAttemptId}`) }).click();
  await expect(page.getByRole("dialog")).toContainText("codex.implementer_resume");
  expect(operations.filter((call) =>
    ["run.initial-prompt", "run.review", "run.review-content", "run.output"].includes(
      call.operation,
    ),
  )).toHaveLength(sensitiveBefore);
  await page.getByRole("button", { name: "Load initial run prompt (context)" }).click();
  await expect(page.locator(".content-text")).toContainText("Implement the sample plan exactly as written");
  await page.getByRole("button", { name: "Load stdout" }).click();
  await expect(page.locator(".content-text")).not.toBeEmpty();
  await page.getByRole("button", { name: "Load stderr" }).click();
  await expect(page.getByText("Process complete")).toBeVisible();
  await page.getByRole("button", { name: "Close attempt details" }).click();

  await page.getByRole("button", { name: new RegExp(`Attempt ID: ${fixture.historicalReviewAttemptId}`) }).click();
  await page.getByRole("button", { name: "Load exact review prompt" }).click();
  await expect(page.getByText(/Unavailable: not_recorded/)).toBeVisible();
  await page.getByRole("button", { name: "Close attempt details" }).click();

  await page.getByRole("button", { name: new RegExp(`Attempt ID: ${fixture.exactReviewAttemptId}`) }).click();
  await page.getByRole("button", { name: "Load exact review prompt" }).click();
  await expect(page.locator(".content-text")).not.toBeEmpty();
  await page.getByRole("button", { name: "Close attempt details" }).click();
  expect(
    operations.some(
      (call) =>
        call.operation === "run.review-content" &&
        call.parameters.attemptId === fixture.implementerAttemptId,
    ),
  ).toBe(false);

  await selectRun(page, fixture.sourceRunId);
  // Replacing the run remounts attempt details closed.
  await page.locator(".attempt-details-disclosure > summary").click();
  await page.getByRole("button", { name: new RegExp(`Attempt ID: ${fixture.activeAttemptId}`) }).click();
  await page.getByRole("button", { name: "Load stdout" }).click();
  await expect(page.getByText("no / active")).toBeVisible();
  await expect(page.getByRole("button", { name: "Check for new bytes" })).toBeVisible();
  await page.getByRole("button", { name: "Close attempt details" }).click();

  const readsBeforePause = operations.length;
  await page.getByRole("button", { name: "Pause automatic refresh" }).click();
  await page.getByRole("button", { name: "Refresh current scope" }).click();
  await expect.poll(() => operations.length).toBeGreaterThan(readsBeforePause);
  await expect(page.getByRole("button", { name: "Resume automatic refresh" })).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });

  await page.getByRole("button", { name: "Use dark theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([
    "ai-dev-loop-dashboard-theme",
  ]);
  expect(externalRequests).toEqual([]);
});
